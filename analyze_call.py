#!/usr/bin/env python3
"""
Per-turn latency breakdown for a conversation.

    python analyze_call.py                 # list recent conversations
    python analyze_call.py conv_xxxx       # break down one call

Read `ttf_audio_since_silence`: the time from the user going quiet to the first
audio out. That is the latency a caller actually feels.

Two traps when reading latency:

  * Wall-clock timestamps (in this output and in the ElevenLabs dashboard) mark
    when an utterance STARTED. A turn logged at 0:03 that answers at 0:10 is not
    a 7s delay -- the user was still talking. Only the metrics tell the truth.
  * `ttf_audio_since_silence` is measured from the end of the previous turn, so
    if the user sat quiet for 13s before speaking, it reports ~14s. Trust it only
    on turns where the user replied promptly.
"""

import json
import os
import sys
import urllib.request

import common  # loads .env on import

ROOT = os.path.dirname(os.path.abspath(__file__))


KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()


def get(path):
    req = urllib.request.Request("https://api.elevenlabs.io/v1" + path,
                                 headers={"xi-api-key": KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main():
    if not KEY:
        print("  ELEVENLABS_API_KEY missing from .env")
        return 1

    cid = sys.argv[1] if len(sys.argv) > 1 else None
    if not cid:
        lst = get("/convai/conversations?agent_id=%s&page_size=10" % AGENT)
        print("")
        for c in lst.get("conversations", []):
            print("  %s  %-8s %2s msgs" % (
                c["conversation_id"], c.get("status"), c.get("message_count")))
        print("\n  Pass a conversation id to break it down.\n")
        return 0

    d = get("/convai/conversations/" + cid)
    meta = d.get("metadata") or {}
    print("")
    print("  status      : %s" % d.get("status"))
    print("  duration    : %ss" % meta.get("call_duration_secs"))
    print("  termination : %s" % meta.get("termination_reason"))
    if meta.get("error"):
        print("  error       : %s" % json.dumps(meta["error"]))
    print("")

    tool_times = []
    perceived = []

    print("  %-5s %-6s %-8s %s" % ("t(s)", "role", "felt", "detail"))
    print("  " + "-" * 92)
    for t in d.get("transcript", []):
        secs = t.get("time_in_call_secs")
        role = t.get("role")
        msg = (t.get("message") or "").replace("\n", " ")
        metrics = ((t.get("conversation_turn_metrics") or {}).get("metrics") or {})

        def val(name):
            entry = metrics.get(name)
            return entry.get("elapsed_time") if isinstance(entry, dict) else None

        felt = val("convai_ttf_audio_since_silence")
        if felt is not None and role == "agent":
            perceived.append((secs, felt))
        print("  %-5s %-6s %-8s %s" % (
            secs, role, ("%.2fs" % felt) if felt is not None else "-", msg[:70]))

        parts = []
        for label, key in (("silence", "convai_turn_silence_before_initiation"),
                           ("asr", "convai_turn_asr_latency"),
                           ("llm", "convai_llm_service_ttfb"),
                           ("tts", "convai_tts_service_ttfb")):
            v = val(key)
            if v is not None:
                parts.append("%s=%.2f" % (label, v))
        if parts:
            print("        %s" % "  ".join(parts))

        for res in (t.get("tool_results") or []):
            lat = res.get("tool_latency_secs")
            if lat is not None:
                tool_times.append(lat)
            print("        tool %s -> %.2fs %s" % (
                res.get("tool_name"), lat or 0,
                "ERROR" if res.get("is_error") else ""))

    print("")
    if perceived:
        vals = [p for _, p in perceived]
        print("  agent turns measured : %d" % len(vals))
        print("  fastest / median     : %.2fs / %.2fs" % (
            min(vals), sorted(vals)[len(vals) // 2]))
    if tool_times:
        print("  tool calls           : %d, avg %.2fs, max %.2fs" % (
            len(tool_times), sum(tool_times) / len(tool_times), max(tool_times)))
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

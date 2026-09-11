#!/usr/bin/env python3
"""
Run several phrasings past the agent and report which tool each one reached.

    python test_phrasings.py                       # the built-in set
    python test_phrasings.py "any hoisin sauce?" "got fish sauce?"
    python test_phrasings.py --file phrasings.txt  # one per line

Run this after changing a tool description or adding a tool. Tool selection is
the part that degrades quietly: nothing errors, the agent just starts answering
from the wrong table, and more tools make it likelier.

The line to read is "routed to" -- the tool chosen and the value sent to it. A
miss is far more often a bad search term than a bad lookup.

Exits non-zero if any phrasing came back with no rows, so it can gate a change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import convai

DEFAULT_PHRASINGS = [
    "How is customer RED005 doing?",
    "Which accounts are spending the most?",
    "Do you have PadThai noodles?",
    "How much of part number 11050 can we ship?",
]


def main() -> int:
    if not convai.configured():
        print("  ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must be set in .env")
        return 1

    args = sys.argv[1:]
    if args and args[0] == "--file":
        if len(args) < 2:
            print("  usage: python test_phrasings.py --file phrasings.txt")
            return 1
        phrasings = [ln.strip() for ln in Path(args[1]).read_text(encoding="utf-8").splitlines()
                     if ln.strip() and not ln.startswith("#")]
    else:
        phrasings = args or DEFAULT_PHRASINGS

    print("\n  Running %d phrasing(s). Each is a real conversation, so allow ~15s"
          " apiece.\n" % len(phrasings))

    misses = 0
    for phrase in phrasings:
        result = convai.converse([phrase])
        calls = convai.tool_calls(result["conversation_id"]) if result["conversation_id"] else []
        rows = sum(c["rows"] for c in calls)
        routed = " -> ".join("%s(%s)" % (c["tool"], ", ".join(repr(v) for v in c["values"]))
                             for c in calls)

        ok = rows > 0
        if not ok:
            misses += 1
        print("  %s  %s" % ("PASS" if ok else "MISS", phrase))
        print("        routed to : %s" % (routed or "(no tool call)"))
        print("        rows      : %d" % rows)
        if result["replies"]:
            print("        said      : %s" % result["replies"][-1][:92])
        if result["error"]:
            print("        error     : %s" % result["error"])
        print("")

    print("  %d/%d found something.\n" % (len(phrasings) - misses, len(phrasings)))
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())

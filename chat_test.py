#!/usr/bin/env python3
"""
Talk to the agent in text mode -- no microphone, no browser, no dependencies.

    python chat_test.py "What do you sell?"
    python chat_test.py "Pad Thai noodles" "What about the sauce?"
    python chat_test.py --file script.txt        # one turn per line
    python chat_test.py --rep "Ryan Tran" "How am I doing?"   # as that rep

Give it several messages to test a conversation rather than a question. Context
failures only show up across turns: "what about the sauce?" behaves correctly on
its own and wrongly as a follow-up, so a single-turn test cannot catch it.

Prints the search terms the model actually sent, which is usually what you want
to see -- a wrong answer is far more often a bad search term than a bad lookup.

Exit code is 0 only if the agent replied after one of your messages. The opening
line is static configuration and does not prove the LLM ran, so it is not
counted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import convai


def main() -> int:
    if not convai.configured():
        print("  ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must be set in .env")
        return 1

    args = sys.argv[1:]
    # The web page sends whoever is picked in the rep dropdown; this is how to
    # test as one of them rather than as a manager looking at everything.
    rep = ""
    if len(args) >= 2 and args[0] == "--rep":
        rep, args = args[1], args[2:]
    if args and args[0] == "--file":
        if len(args) < 2:
            print("  usage: python chat_test.py --file script.txt")
            return 1
        script = [ln.strip() for ln in Path(args[1]).read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]
    else:
        script = args or ["What do you help people with?"]

    if rep:
        print(r"\n  signed in as: %s" % rep)
    result = convai.converse(
        script,
        rep=rep,
        on_user=lambda text: print("\n  you   : %s" % text),
        on_agent=lambda text, after_user: print(
            "  agent%s: %s" % ("  " if after_user else " (opener)", text)),
    )

    cid = result["conversation_id"]
    if cid:
        print("\n  conversation: %s" % cid)
        for call in convai.tool_calls(cid):
            print("      %-22s %-30s %d row(s)" % (
                call["tool"], ", ".join(repr(v) for v in call["values"]), call["rows"]))

    replied = bool(result["replies"])
    print("\n  RESULT: %s" % ("the agent replied" if replied
                              else "no reply after your message -- generation failed"))
    if result["error"]:
        print("  socket: %s" % result["error"])

    if cid:
        info = convai.outcome(cid)
        if info.get("status"):
            print("  status            : %s" % info["status"])
            print("  termination_reason: %s" % info.get("termination_reason"))
            if info.get("error"):
                print("  error             : %s" % info["error"])
    print("")
    return 0 if replied else 1


if __name__ == "__main__":
    raise SystemExit(main())

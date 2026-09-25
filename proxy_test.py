#!/usr/bin/env python3
"""
Talk to the LLM proxy on this machine -- no deployment, no ElevenLabs.

    python proxy_test.py "How am I doing this month?"
    python proxy_test.py --rep "Ryan Tran" "Who should I call?" "And the first one?"
    python proxy_test.py --port 8080                 # use a server already running

Starts a server on a spare port, sends the same request ElevenLabs would, and
prints the answer with the lookups it ran underneath. Then stops it again.

This exercises everything the deployed proxy does: the prompt, the tool loop,
the Databricks queries and the per-rep scoping. The single thing it cannot
cover is ElevenLabs reaching this machine, which no amount of local testing
will tell you about -- that needs a public URL.

Pass --rep to be that rep. Without it you are a manager looking across the
whole company, which is what the "All reps" option in the page means.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import common  # loads .env on import
import auth

ROOT = Path(__file__).resolve().parent
PROMPT_FILE = ROOT / "agent_prompt.md"


def wait_for(port: int, seconds: float = 25.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/login" % port, timeout=2).read()
            return True
        except urllib.error.HTTPError:
            return True          # answering at all is enough
        except Exception:        # noqa: BLE001 - still starting
            time.sleep(0.4)
    return False


def ask(port: int, messages: list, rep: str, secret: str, model: str) -> str:
    body = json.dumps({
        "model": model,
        "max_tokens": 512,
        "messages": messages,
        "scope_token": auth.issue_scope(rep),
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/llm/v1/chat/completions" % port,
        data=body, method="POST",
        headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"})
    out = []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            delta = ((data.get("choices") or [{}])[0].get("delta") or {})
            out.append(delta.get("content") or "")
    return "".join(out).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the LLM proxy locally.")
    parser.add_argument("--rep", default="", help="sign in as this rep")
    parser.add_argument("--port", type=int, default=0,
                        help="a server already running; otherwise one is started")
    parser.add_argument("messages", nargs="*", help="one turn each")
    args = parser.parse_args()

    secret = os.environ.get("LLM_PROXY_SECRET", "").strip()
    model = os.environ.get("CUSTOM_LLM_MODEL", "").strip()
    if not secret:
        print("  LLM_PROXY_SECRET is missing from .env -- the proxy rejects every call")
        return 1
    if not PROMPT_FILE.is_file():
        print("  %s not found" % PROMPT_FILE.name)
        return 1

    turns = args.messages or ["How am I doing this month?"]
    port, server = args.port, None
    if not port:
        port = 8791
        # Its stderr is the point: the tool lines show which lookup ran, with
        # what value, as whom, and how many rows came back.
        server = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "server.py"),
             "--port", str(port), "--no-browser"],
            stdout=subprocess.DEVNULL, stderr=None)
        print("\n  started a server on port %d" % port)
        if not wait_for(port):
            server.terminate()
            print("  it never came up")
            return 1

    print("  signed in as: %s\n" % (args.rep or "All reps (manager)"))
    # ElevenLabs substitutes dynamic variables into the prompt before it sends
    # it. Nothing here does, so do it, or the model reads a literal
    # "{{rep_name}}" and the test is not testing what runs in production.
    system = PROMPT_FILE.read_text(encoding="utf-8").replace(
        "{{rep_name}}", args.rep or "All")
    messages = [{"role": "system", "content": system}]
    failed = False
    try:
        for turn in turns:
            print("  you   : %s" % turn)
            messages.append({"role": "user", "content": turn})
            try:
                answer = ask(port, messages, args.rep, secret, model)
            except urllib.error.HTTPError as exc:
                print("  HTTP %s: %s\n" % (exc.code, exc.read().decode()[:200]))
                failed = True
                break
            messages.append({"role": "assistant", "content": answer})
            print("  agent : %s\n" % (answer or "(no answer)"))
            failed = failed or not answer
    finally:
        if server:
            server.terminate()
            server.wait(timeout=10)
            print("  stopped the server")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

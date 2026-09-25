#!/usr/bin/env python3
"""
Switch the agent between two ways of reaching Databricks.

    python configure_proxy.py                     # which one is live now
    python configure_proxy.py --on https://host   # route through this server
    python configure_proxy.py --off               # back to webhook tools

**direct** (the default): ElevenLabs holds a Databricks token, calls the
warehouse itself through webhook tools, and sees every row that comes back.
Nothing needs to reach this server, so it works from a laptop.

**proxy**: ElevenLabs calls this server as an ordinary OpenAI-compatible model
and never learns a tool exists. The tool loop, the Databricks token and every
row stay here; only the spoken answer leaves. That is also the only way a
lookup can be limited to the rep who asked, because the scope token travels
with the request and nothing on ElevenLabs' side knows who is on the phone.

The cost is that this server joins the live call path. A restart mid-call ends
the call, and the host has to be reachable from the public internet.

Switching back is one command and changes nothing here, so it is safe to try.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import common  # loads .env on import

EL_API = "https://api.elevenlabs.io/v1"
EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()
PROXY_SECRET = os.environ.get("LLM_PROXY_SECRET", "").strip()
PROXY_SECRET_NAME = os.environ.get("LLM_PROXY_SECRET_NAME", "LLM_PROXY_BEARER").strip()
UPSTREAM_MODEL = os.environ.get("CUSTOM_LLM_MODEL", "").strip()
DIRECT_URL = os.environ.get("CUSTOM_LLM_URL", "").strip()


# What the agent looked like before the switch, so --off puts back exactly
# that. Rebuilding it from .env was not enough: api_key is a reference to a
# secret stored at ElevenLabs, and request_headers carry a User-Agent the CDN
# in front of the LLM insists on. Neither is reconstructable from anything
# here, and dropping them left the agent authenticating to the LLM with the
# proxy's own bearer -- every call failed, for a reason nothing reported.
STATE_FILE = common.ROOT / ".proxy-previous.json"


class Fail(Exception):
    pass


def el(path: str, method: str = "GET", body: dict | None = None) -> dict:
    if not EL_KEY:
        raise Fail("ELEVENLABS_API_KEY is missing from .env")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"xi-api-key": EL_KEY, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(EL_API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raise Fail("HTTP %d on %s %s\n    %s"
                   % (exc.code, method, path,
                      exc.read().decode("utf-8", "replace")[:400])) from exc


def agent() -> dict:
    if not AGENT_ID:
        raise Fail("ELEVENLABS_AGENT_ID is missing from .env")
    return el("/convai/agents/" + urllib.parse.quote(AGENT_ID))


def prompt_of(a: dict) -> dict:
    return a.get("conversation_config", {}).get("agent", {}).get("prompt", {}) or {}


def ensure_secret(value: str) -> str:
    """Store the proxy bearer as a workspace secret, so it is not in the URL."""
    existing = el("/convai/secrets").get("secrets", [])
    match = next((s for s in existing if s.get("name") == PROXY_SECRET_NAME), None)
    if match:
        el("/convai/secrets/" + match["secret_id"], "PATCH",
           {"type": "update", "name": PROXY_SECRET_NAME, "value": value})
        return match["secret_id"]
    return el("/convai/secrets", "POST",
              {"type": "new", "name": PROXY_SECRET_NAME, "value": value})["secret_id"]


def describe(a: dict) -> None:
    pr = prompt_of(a)
    custom = pr.get("custom_llm") or {}
    url = custom.get("url") or ""
    tools = pr.get("tool_ids") or []
    mode = "proxy" if "/llm/v1" in url else "direct"
    print("\n  mode      : %s" % mode)
    print("  llm url   : %s" % (url or "(built-in model)"))
    print("  webhook tools attached : %d" % len(tools))
    if mode == "proxy" and tools:
        print("\n  Both at once: ElevenLabs is calling this server AND still holds")
        print("  webhook tools, so it can still reach Databricks directly. Run")
        print("  --on again to detach them.")
    if mode == "direct":
        print("\n  Lookups run from ElevenLabs. Per-rep scoping is not possible in")
        print("  this mode -- nothing in that path knows who is signed in.")


def turn_on(base_url: str) -> None:
    if not PROXY_SECRET:
        raise Fail("LLM_PROXY_SECRET is missing from .env -- the proxy would reject "
                   "every call from ElevenLabs")
    if not UPSTREAM_MODEL:
        raise Fail("CUSTOM_LLM_MODEL is missing from .env")
    url = base_url.rstrip("/")
    if not url.startswith("https://"):
        raise Fail("The URL must be https -- ElevenLabs will not post a bearer "
                   "token over plain http, and this one authenticates the caller")
    if not url.endswith("/llm/v1"):
        url = url + "/llm/v1"

    print("\n  Pointing the agent at %s" % url)
    secret_id = ensure_secret("Bearer " + PROXY_SECRET)
    print("  secret    : %s (%s)" % (PROXY_SECRET_NAME, secret_id))

    before = agent()
    previous = prompt_of(before)
    STATE_FILE.write_text(json.dumps({
        "custom_llm": previous.get("custom_llm") or {},
        "llm": previous.get("llm"),
        "tool_ids": previous.get("tool_ids") or [],
    }, indent=2), encoding="utf-8")
    print("  saved     : previous LLM settings -> %s" % STATE_FILE.name)

    el("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
       {"conversation_config": {"agent": {"prompt": {
           "llm": "custom-llm",
           "custom_llm": {
               "url": url,
               "model_id": UPSTREAM_MODEL,
               "api_key": {"secret_id": secret_id},
               "api_type": "chat_completions",
           },
           # Detached, not deleted. ElevenLabs must not be able to reach the
           # warehouse itself once this server is doing the looking up --
           # otherwise the scoping is one tool call away from being bypassed.
           "tool_ids": [],
       }}}})
    kept = len(prompt_of(before).get("tool_ids") or [])
    print("  detached  : %d webhook tool(s) -- the proxy supplies tools itself" % kept)
    describe(agent())


def turn_off() -> None:
    if not STATE_FILE.is_file():
        raise Fail("No saved settings (%s). --off restores what --on recorded; "
                   "without it, put the agent back with:\n"
                   "      python configure_llm.py --apply\n"
                   "      python databricks_tool.py --sync" % STATE_FILE.name)
    saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    custom = saved.get("custom_llm") or {}
    if not custom.get("url"):
        raise Fail("%s records no previous url" % STATE_FILE.name)

    print("\n  Restoring the agent to %s" % custom.get("url"))
    el("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
       {"conversation_config": {"agent": {"prompt": {
           "llm": saved.get("llm") or "custom-llm",
           "custom_llm": custom,
           "tool_ids": saved.get("tool_ids") or [],
       }}}})
    print("  restored  : %d webhook tool(s)" % len(saved.get("tool_ids") or []))
    STATE_FILE.unlink()
    describe(agent())


def main() -> int:
    parser = argparse.ArgumentParser(description="Route the agent's lookups through "
                                                 "this server, or back to ElevenLabs.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", metavar="URL",
                       help="public https base URL of this server")
    group.add_argument("--off", action="store_true",
                       help="restore direct webhook tools")
    args = parser.parse_args()
    try:
        if args.on:
            turn_on(args.on)
        elif args.off:
            turn_off()
        else:
            describe(agent())
            print("\n  --on https://host to route through this server, --off to undo.\n")
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

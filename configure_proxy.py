#!/usr/bin/env python3
"""
Switch the agent between two ways of reaching Databricks.

    python configure_proxy.py                     # which one is live now
    python configure_proxy.py --on https://host   # route through this server
    python configure_proxy.py --off               # back to webhook tools

An ElevenLabs workspace has one agent and every deployment points at it, so
this changes production as readily as it changes a laptop -- a branch cannot
isolate it and a git revert cannot undo it. Work against a copy named
*-test, which is what ELEVENLABS_AGENT_ID should point at while developing;
anything else is refused unless you pass --force.

**direct** (the default): ElevenLabs holds a Databricks token, calls the
warehouse itself through webhook tools, and sees every row that comes back.
Nothing needs to reach this server, so it works from a laptop.

**proxy**: ElevenLabs calls this server as an ordinary OpenAI-compatible model
and never learns a tool exists. The tool loop, the Databricks token and every
row stay here; only the spoken answer leaves, and the workspace secret holding
the Databricks token is deleted, so ElevenLabs has no way to reach the
warehouse rather than merely no reason to.

It is also the only arrangement where a lookup can be limited to the rep who
asked: the scope token travels with the request, and nothing on ElevenLabs'
side knows who is on the phone.

What it does not change is the transcript. ElevenLabs still stores what was
said, and a spoken answer names accounts and figures -- so this moves the
exposure from whole result sets to the sentences read out, not to nothing.

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
DBX_SECRET_NAME = os.environ.get("DATABRICKS_SECRET_NAME", "DATABRICKS_BEARER").strip()


# What the agent looked like before the switch, so --off puts back exactly
# that. Rebuilding it from .env was not enough: api_key is a reference to a
# secret stored at ElevenLabs, and request_headers carry a User-Agent the CDN
# in front of the LLM insists on. Neither is reconstructable from anything
# here, and dropping them left the agent authenticating to the LLM with the
# proxy's own bearer -- every call failed, for a reason nothing reported.
STATE_FILE = common.ROOT / ".proxy-previous.json"


FORCE = False


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
    overrides = (a.get("platform_settings") or {}).get("overrides") or {}
    print("\n  mode      : %s" % mode)
    print("  extra body allowed : %s" % overrides.get("custom_llm_extra_body"))
    print("  llm url   : %s" % (url or "(built-in model)"))
    print("  webhook tools attached : %d" % len(tools))
    if mode == "proxy" and tools:
        print("\n  Both at once: ElevenLabs is calling this server AND still holds")
        print("  webhook tools, so it can still reach Databricks directly. Run")
        print("  --on again to detach them.")
    if mode == "direct":
        print("\n  Lookups run from ElevenLabs. Per-rep scoping is not possible in")
        print("  this mode -- nothing in that path knows who is signed in.")


def guard_not_production(a: dict, action: str) -> None:
    """Refuse to switch an agent that is not obviously a test copy.

    There is one agent per workspace and every deployment points at it, so
    switching it here switches it for production too -- something a branch
    cannot isolate and a git revert cannot undo. That happened: prod spent a
    while routed through a laptop tunnel that was about to disappear.

    The name is the check, because it is the thing a person reads before
    running this. --force is there for the deliberate cutover.
    """
    name = a.get("name") or ""
    if name.endswith("-test") or FORCE:
        return
    raise Fail("%r does not look like a test agent, so %s was refused.\n"
               "    One agent serves every deployment: switching it here switches\n"
               "    it for production, whatever branch you are on.\n"
               "    Work against a copy (ELEVENLABS_AGENT_ID in .env), or pass\n"
               "    --force if this really is the cutover." % (name, action))


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

    before_check = agent()
    guard_not_production(before_check, "--on")
    print("\n  Pointing %s at %s" % (before_check.get("name"), url))

    # The scope token travels as an extra body field, and an agent refuses
    # those unless told to allow them -- with a close code and a message the
    # caller hears as the call simply ending. Turned on here rather than left
    # as a step to discover by having a call drop.
    el("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
       {"platform_settings": {"overrides": {"custom_llm_extra_body": True}}})
    print("  allowed   : custom_llm_extra_body (carries the scope token)")

    # Bare, not "Bearer <secret>": ElevenLabs adds the scheme itself for a
    # custom LLM. Storing it with the prefix sent "Bearer Bearer ..." and the
    # proxy rejected every call.
    secret_id = ensure_secret(PROXY_SECRET)
    print("  secret    : %s (%s)" % (PROXY_SECRET_NAME, secret_id))

    before = agent()
    if STATE_FILE.is_file():
        # Running --on twice would record the proxy settings as the thing to
        # go back to, leaving --off restoring a tunnel that no longer exists.
        # The first save is the one worth keeping.
        print("  kept      : existing rollback point in %s" % STATE_FILE.name)
    previous = prompt_of(before)
    if not STATE_FILE.is_file():
        STATE_FILE.write_text(json.dumps({
        "custom_llm": previous.get("custom_llm") or {},
        "llm": previous.get("llm"),
        "tool_ids": previous.get("tool_ids") or [],
        }, indent=2), encoding="utf-8")
        print("  saved     : rollback point -> %s" % STATE_FILE.name)

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

    # Detaching the tools stops ElevenLabs using the warehouse; deleting the
    # secret stops it being able to. A credential it holds but does not use is
    # still a credential it holds, and the point of this switch is that it has
    # no way to reach the data at all.
    for secret in el("/convai/secrets").get("secrets", []):
        if secret.get("name") == DBX_SECRET_NAME:
            el("/convai/secrets/" + secret["secret_id"], "DELETE")
            print("  deleted   : %s -- ElevenLabs no longer holds a Databricks "
                  "credential" % DBX_SECRET_NAME)
            break
    describe(agent())


def turn_off() -> None:
    if not STATE_FILE.is_file():
        raise Fail("No saved settings (%s). --off restores what --on recorded; "
                   "without it, put the agent back with:\n"
                   "      python configure_llm.py --apply\n"
                   "      python databricks_tool.py --sync" % STATE_FILE.name)
    guard_not_production(agent(), "--off")
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
    # Those tools authenticate with a secret --on deleted, so they are pointing
    # at nothing until it is recreated. Say so plainly: a restored tool that
    # quietly fails is worse than one that is obviously missing.
    print("\n  The Databricks secret was deleted when the proxy went on, so the")
    print("  restored tools cannot authenticate yet. Finish with:")
    print("      python databricks_tool.py --sync")
    describe(agent())


def main() -> int:
    parser = argparse.ArgumentParser(description="Route the agent's lookups through "
                                                 "this server, or back to ElevenLabs.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--on", metavar="URL",
                       help="public https base URL of this server")
    group.add_argument("--off", action="store_true",
                       help="restore direct webhook tools")
    parser.add_argument("--force", action="store_true",
                        help="switch an agent that is not named *-test")
    args = parser.parse_args()
    global FORCE
    FORCE = args.force
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

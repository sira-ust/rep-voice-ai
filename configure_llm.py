#!/usr/bin/env python3
"""
Point an ElevenLabs agent at a custom OpenAI-compatible LLM (vLLM, Ollama, LiteLLM...).

Standard library only. Reads settings from .env next to this file.

    python configure_llm.py              # show what the agent uses right now
    python configure_llm.py --test       # probe the custom LLM (models + SSE streaming)
    python configure_llm.py --apply      # switch the agent to the custom LLM
    python configure_llm.py --revert gpt-4o-mini    # go back to a built-in model

Notes
-----
ElevenLabs' servers call your LLM, not your browser -- the URL must be reachable
from the public internet, serve /chat/completions, and stream Server-Sent Events.
Run --test first; it checks exactly that.
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

ROOT = Path(__file__).resolve().parent
EL_API = "https://api.elevenlabs.io/v1"
TIMEOUT = 30

# Cloudflare in front of some LLM hosts 403s the default "Python-urllib/x.y"
# user agent, so send an ordinary browser-ish one.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) elevenlabs-webui/1.0"


# --------------------------------------------------------------- config

def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env")

EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()

LLM_URL = os.environ.get("CUSTOM_LLM_URL", "").strip().rstrip("/")
LLM_MODEL = os.environ.get("CUSTOM_LLM_MODEL", "").strip()
LLM_KEY = os.environ.get("CUSTOM_LLM_API_KEY", "").strip()
SECRET_NAME = os.environ.get("CUSTOM_LLM_SECRET_NAME", "CUSTOM_LLM_API_KEY").strip()
MAX_TOKENS = int(os.environ.get("CUSTOM_LLM_MAX_TOKENS", "512") or 512)
LLM_USER_AGENT = os.environ.get("CUSTOM_LLM_USER_AGENT", "").strip()


class Fail(Exception):
    pass


def die(message: str) -> "None":
    raise Fail(message)


# --------------------------------------------------------------- http

def request(url: str, method: str = "GET", headers: dict | None = None, body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    head = {"Accept": "application/json", "User-Agent": UA}
    if data is not None:
        head["Content-Type"] = "application/json"
    head.update(headers or {})

    req = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:600]
        die("%s %s -> HTTP %d\n    %s" % (method, url, exc.code, detail))
    except urllib.error.URLError as exc:
        die("%s %s -> unreachable: %s" % (method, url, exc.reason))


def elevenlabs(path: str, method: str = "GET", body: dict | None = None):
    if not EL_KEY:
        die("ELEVENLABS_API_KEY is missing from .env")
    return request(EL_API + path, method, {"xi-api-key": EL_KEY}, body)


# --------------------------------------------------------------- custom LLM probe

def probe_llm() -> list[str]:
    """Check the custom LLM is reachable, lists models, and streams SSE."""
    if not LLM_URL:
        die("CUSTOM_LLM_URL is missing from .env")

    auth = {"Authorization": "Bearer " + LLM_KEY} if LLM_KEY else {}

    print("  URL      : " + LLM_URL)
    models: list[str] = []
    try:
        data = request(LLM_URL + "/models", headers=auth)
        models = [m.get("id") for m in data.get("data", []) if m.get("id")]
        print("  models   : " + (", ".join(models) if models else "(none returned)"))
    except Fail as exc:
        print("  models   : FAILED\n    %s" % exc)

    model = LLM_MODEL or (models[0] if models else None)
    if not model:
        die("No model to test. Set CUSTOM_LLM_MODEL in .env")
    print("  model    : " + model)

    if LLM_MODEL and models and LLM_MODEL not in models:
        print("  WARNING  : '%s' is not in the served model list" % LLM_MODEL)

    # Streaming check -- ElevenLabs requires text/event-stream.
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
        "stream": True,
        "max_tokens": 16,
    }
    head = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": UA}
    head.update(auth)
    req = urllib.request.Request(
        LLM_URL + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=head,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            print("  stream   : HTTP %d, Content-Type: %s" % (resp.status, ctype))
            if "text/event-stream" not in ctype:
                print("  WARNING  : not an SSE stream -- ElevenLabs requires text/event-stream")
            text = []
            for _ in range(40):
                line = resp.readline().decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line == "data: [DONE]":
                    break
                if line.startswith("data: "):
                    try:
                        chunk = json.loads(line[6:])
                        piece = chunk["choices"][0].get("delta", {}).get("content")
                        if piece:
                            text.append(piece)
                    except (ValueError, KeyError, IndexError):
                        pass
            print("  reply    : %s" % ("".join(text).strip() or "(empty)"))
    except urllib.error.HTTPError as exc:
        die("chat/completions -> HTTP %d\n    %s" % (exc.code, exc.read().decode("utf-8", "replace")[:400]))
    except urllib.error.URLError as exc:
        die("chat/completions -> unreachable: %s" % exc.reason)

    return models


# --------------------------------------------------------------- agent helpers

def get_agent() -> dict:
    if not AGENT_ID:
        die("ELEVENLABS_AGENT_ID is missing from .env")
    return elevenlabs("/convai/agents/" + AGENT_ID)


def prompt_of(agent: dict) -> dict:
    return agent.get("conversation_config", {}).get("agent", {}).get("prompt", {}) or {}


def show(agent: dict | None = None) -> None:
    agent = agent or get_agent()
    prompt = prompt_of(agent)
    custom = prompt.get("custom_llm") or {}

    print("  agent    : %s (%s)" % (agent.get("name", "?"), AGENT_ID))
    print("  llm      : %s" % prompt.get("llm"))
    if custom:
        key_ref = custom.get("api_key") or {}
        print("  url      : %s" % custom.get("url"))
        print("  model_id : %s" % custom.get("model_id"))
        print("  api_type : %s" % custom.get("api_type", "chat_completions"))
        print("  api_key  : %s" % (key_ref.get("secret_id") or key_ref.get("env_var_label") or "(none)"))
        headers = custom.get("request_headers") or {}
        print("  headers  : %s" % (headers if headers else "(none)"))
    print("  temp     : %s   max_tokens: %s" % (prompt.get("temperature"), prompt.get("max_tokens")))


def ensure_secret(update: bool) -> str:
    """Create (or reuse) the workspace secret holding the custom LLM key."""
    if not LLM_KEY:
        die("CUSTOM_LLM_API_KEY is missing from .env")

    existing = elevenlabs("/convai/secrets").get("secrets", [])
    match = next((s for s in existing if s.get("name") == SECRET_NAME), None)

    if match:
        secret_id = match["secret_id"]
        if update:
            elevenlabs(
                "/convai/secrets/" + secret_id,
                "PATCH",
                {"type": "update", "name": SECRET_NAME, "value": LLM_KEY},
            )
            print("  secret   : updated '%s' (%s)" % (SECRET_NAME, secret_id))
        else:
            print("  secret   : reusing '%s' (%s)" % (SECRET_NAME, secret_id))
        return secret_id

    created = elevenlabs(
        "/convai/secrets", "POST", {"type": "new", "name": SECRET_NAME, "value": LLM_KEY}
    )
    print("  secret   : created '%s' (%s)" % (SECRET_NAME, created["secret_id"]))
    return created["secret_id"]


def patch_prompt(fields: dict) -> dict:
    return elevenlabs(
        "/convai/agents/" + AGENT_ID,
        "PATCH",
        {"conversation_config": {"agent": {"prompt": fields}}},
    )


# --------------------------------------------------------------- commands

def cmd_apply(args) -> None:
    print("\nChecking the custom LLM")
    models = probe_llm()
    model = LLM_MODEL or models[0]

    print("\nCurrent agent config")
    before = get_agent()
    show(before)
    previous_llm = prompt_of(before).get("llm")

    print("\nApplying")
    secret_id = ensure_secret(update=args.update_secret)

    custom_llm = {
        "url": LLM_URL,
        "model_id": model,
        "api_key": {"secret_id": secret_id},
        "api_type": "chat_completions",
        # A Cloudflare (or similar) WAF in front of the LLM host can silently
        # block ElevenLabs' server-to-server request by its User-Agent, which
        # then surfaces only as a generic "custom_llm generation failed" --
        # indistinguishable from an actual model error. Send a browser-like UA
        # to avoid that, if one is configured.
        "request_headers": {"User-Agent": LLM_USER_AGENT} if LLM_USER_AGENT else {},
    }
    fields = {"llm": "custom-llm", "custom_llm": custom_llm}

    # ElevenLabs' "unlimited" sentinel is max_tokens = -1, which vLLM rejects with
    # "max_tokens must be at least 1". Replace it with a real cap.
    current_max = prompt_of(before).get("max_tokens")
    if current_max is None or current_max < 1:
        print("  max_tok  : %s is invalid for vLLM, setting %d" % (current_max, MAX_TOKENS))
        fields["max_tokens"] = MAX_TOKENS

    patch_prompt(fields)

    print("\nAgent config after")
    after = get_agent()
    show(after)

    if prompt_of(after).get("llm") != "custom-llm":
        die("The PATCH did not stick -- llm is still '%s'" % prompt_of(after).get("llm"))

    print("\n  Done. Previous model was '%s'." % previous_llm)
    print("  To undo:  python configure_llm.py --revert %s" % previous_llm)


def cmd_simulate(args) -> None:
    """Run a real text conversation through ElevenLabs against the agent's LLM."""
    print("\nSimulating a conversation (no microphone needed)")
    show()
    body = {
        "simulation_specification": {
            "simulated_user_config": {
                "first_message": "Hi, quick question about your product.",
                "prompt": {
                    "prompt": (
                        "You are a curious prospective customer. Ask one short question, "
                        "then thank them and end the conversation. Keep replies under 15 words."
                    )
                },
            }
        },
        "new_turns_limit": args.turns,
    }

    global TIMEOUT
    TIMEOUT = 180  # simulations run several LLM turns
    result = elevenlabs(
        "/convai/agents/" + urllib.parse.quote(AGENT_ID) + "/simulate-conversation",
        "POST",
        body,
    )

    turns = result.get("simulated_conversation") or []
    print("\n  transcript (%d turns)" % len(turns))
    for turn in turns:
        message = turn.get("message")
        print("    [%-5s] %s" % (turn.get("role"), message if message else "(no message)"))

    analysis = result.get("analysis") or {}
    if analysis:
        print("\n  call_successful   : %s" % analysis.get("call_successful"))
        summary = analysis.get("transcript_summary")
        if summary:
            print("  summary           : %s" % summary[:300])

    if not turns:
        die("The simulation produced no turns -- the LLM call is still failing.")
    if not any(t.get("role") == "agent" and t.get("message") for t in turns):
        die("The agent never produced a message -- the custom LLM is still failing.")
    print("\n  The agent generated replies through the custom LLM.")


def cmd_revert(args) -> None:
    print("\nCurrent agent config")
    show()
    patch_prompt({"llm": args.revert, "custom_llm": None})
    print("\nAgent config after")
    show()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Point an ElevenLabs agent at a custom OpenAI-compatible LLM.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="switch the agent to the custom LLM")
    group.add_argument("--test", action="store_true", help="probe the custom LLM only, change nothing")
    group.add_argument("--revert", metavar="MODEL", help="restore a built-in model, e.g. gpt-4o-mini")
    group.add_argument(
        "--simulate",
        action="store_true",
        help="run a text conversation through ElevenLabs to prove the LLM works end to end",
    )
    parser.add_argument("--turns", type=int, default=4, help="turn limit for --simulate")
    parser.add_argument(
        "--update-secret",
        action="store_true",
        help="overwrite the stored secret with CUSTOM_LLM_API_KEY from .env",
    )
    args = parser.parse_args()

    try:
        if args.test:
            print("\nProbing the custom LLM")
            probe_llm()
        elif args.apply:
            cmd_apply(args)
        elif args.simulate:
            cmd_simulate(args)
        elif args.revert:
            cmd_revert(args)
        else:
            print("\nCurrent agent config")
            show()
            print("\n  --test to probe the LLM, --apply to switch the agent over.")
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

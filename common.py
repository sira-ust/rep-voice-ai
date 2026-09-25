#!/usr/bin/env python3
"""
Settings shared by every script here.

Standard library only. Importing this loads .env once, so a script needs only:

    import common                                  # .env is now in os.environ
    KEY = os.environ.get("ELEVENLABS_API_KEY", "")

Real environment variables win over .env, so a systemd unit, a container or CI
can override a value without editing the file. That precedence is deliberate,
but it does surprise people: a stale shell variable will silently shadow an
edit to .env, and every script will keep using the old value. `common.py
--check` prints which of the two is actually in force for each key.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
ENV_TEST_FILE = ROOT / ".env.test"
APP_ENV = os.environ.get("APP_ENV", "").strip().lower()


def _utf8_console() -> None:
    """Let these scripts print non-ASCII on a Windows console.

    A Windows terminal hands Python cp1252, which cannot encode much of what
    these scripts print: a Chinese reply from the agent, or the accents and
    punctuation store names carry out of the source system. Printing one such
    character raises UnicodeEncodeError and takes the whole script down, so a
    lookup returning the wrong account is not the worst case -- a crash
    mid-listing is.

    errors="replace" so an unexpected glyph degrades to a question mark
    instead of ending the run.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Redirected to a pipe or replaced by a test harness: leave it be.
            pass


_utf8_console()

# Keys whose values must never be printed.
SECRET_KEYS = (
    "ELEVENLABS_API_KEY", "DATABRICKS_TOKEN", "DATABRICKS_WARM_TOKEN",
    "CUSTOM_LLM_API_KEY", "APP_SECRET", "APP_USERS",
)


def _parse(path: Path) -> dict:
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key:
            values[key] = value.strip().strip('"').strip("'")
    return values


def load(path: Path | None = None) -> None:
    """Populate os.environ from .env without overriding what is already set.

    Warns when a secret in the environment differs from the one in .env. That
    precedence is deliberate -- a systemd unit or CI must be able to override
    the file -- but it is also how an expired token left in a shell keeps
    winning after the file has been fixed. The failure that produces is a 403
    from a service, pages away from the cause, and it has cost real time here.
    """
    values = _parse(path or ENV_FILE)
    if path is None and APP_ENV == "test" and ENV_TEST_FILE.is_file():
        overlay = _parse(ENV_TEST_FILE)
        values.update(overlay)
        sys.stderr.write("  APP_ENV=test: %s overriding %s\n"
                         % (ENV_TEST_FILE.name, ", ".join(sorted(overlay))))
    shadowed = []
    for key, value in values.items():
        current = os.environ.get(key)
        if current is not None and current != value and key in SECRET_KEYS:
            shadowed.append(key)
        os.environ.setdefault(key, value)
    for key in shadowed:
        sys.stderr.write(
            "  WARNING: %s is set in your environment and differs from .env.\n"
            "           The environment wins, so .env edits have no effect on it.\n"
            "           python common.py --check  shows which source each key uses.\n" % key)


class Fail(Exception):
    """A problem worth telling the operator about, not a stack trace.

    Each script here already has one of these; this is the shared one, so a
    check that lives in common can be caught by whichever script called it.
    """


# ------------------------------------------------------------------ agent guard

EL_API = "https://api.elevenlabs.io/v1"
FORCE_PRODUCTION = os.environ.get("APP_FORCE_PRODUCTION", "").strip().lower() in (
    "1", "true", "on")


def agent_name(agent_id: str = "") -> str:
    """The agent's name, or "" if it cannot be read."""
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    agent_id = agent_id or os.environ.get("ELEVENLABS_AGENT_ID", "").strip()
    if not (key and agent_id):
        return ""
    req = urllib.request.Request(
        EL_API + "/convai/agents/" + urllib.parse.quote(agent_id),
        headers={"xi-api-key": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()).get("name") or ""
    except Exception:  # noqa: BLE001 - the caller decides what a failure means
        return ""


def require_test_agent(action: str, force: bool = False) -> str:
    """Refuse to change an agent that is not obviously a test copy.

    A workspace has one agent and every deployment points at it, so a script
    run on a laptop rewrites production. No branch isolates that and no git
    revert undoes it -- it has already happened here once, and the code and
    the deployment both looked untouched while it had.

    The name is the check because it is what a person reads before pressing
    enter. A name that cannot be read counts as production: a failed lookup is
    not evidence that this is safe.
    """
    if force or FORCE_PRODUCTION:
        return agent_name()
    name = agent_name()
    if name.endswith("-test"):
        return name
    raise Fail(
        "%s would change %s, which is not a test agent.\n"
        "    One agent serves every deployment, so this changes production\n"
        "    whatever branch you are on.\n"
        "    Point ELEVENLABS_AGENT_ID at a copy named *-test, or re-run with\n"
        "    --force if you mean it."
        % (action, ("%r" % name) if name else "an agent whose name could not be read"))


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    return "%s...%s (%d chars)" % (value[:4], value[-4:], len(value)) if len(value) > 12 \
        else "(set, %d chars)" % len(value)


# Loading on import is what lets a script say `import common` and be done.
load()


def _check() -> int:
    """Report, per key, whether .env or a real environment variable is winning."""
    file_values = _parse(ENV_FILE)
    if not file_values:
        print("\n  No .env found at %s\n" % ENV_FILE)
        return 1

    shadowed = []
    print("\n  %-28s %-10s %s" % ("KEY", "SOURCE", "VALUE"))
    print("  " + "-" * 66)
    for key in sorted(file_values):
        from_file = file_values[key]
        live = os.environ.get(key, "")
        source = "shell" if live != from_file else ".env"
        if source == "shell":
            shadowed.append(key)
        shown = mask(live) if key in SECRET_KEYS else (live[:34] or "(empty)")
        print("  %-28s %-10s %s" % (key, source, shown))

    if shadowed:
        print("\n  %d key(s) overridden by the environment, so edits to .env have"
              " no effect:" % len(shadowed))
        for key in shadowed:
            print("      %s" % key)
        print("\n  Clear them, then restart the terminal. On Windows:")
        print("      [Environment]::SetEnvironmentVariable('NAME',$null,'User')")
        print("  On Linux, check ~/.bashrc, ~/.profile and any systemd unit.")
    else:
        print("\n  Every key is coming from .env.")
    print("")
    return 2 if shadowed else 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        raise SystemExit(_check())
    print(__doc__)

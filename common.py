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

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"


def _utf8_console() -> None:
    """Let these scripts print non-ASCII on a Windows console.

    A Windows terminal hands Python cp1252, which cannot encode a Chinese
    character: printing one raises UnicodeEncodeError and takes the script
    down. Now that the agent answers in Chinese, that turned every test run
    against a Chinese phrase into a crash rather than a result.

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
    """Populate os.environ from .env without overriding what is already set."""
    for key, value in _parse(path or ENV_FILE).items():
        os.environ.setdefault(key, value)


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

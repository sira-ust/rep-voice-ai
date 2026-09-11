#!/usr/bin/env python3
"""
Check ElevenLabs subscription usage (characters used / remaining / reset date).

Standard library only. Reads ELEVENLABS_API_KEY from .env next to this file.

    python check_usage.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EL_API = "https://api.elevenlabs.io/v1"
TIMEOUT = 20


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


def get(path: str) -> dict:
    req = urllib.request.Request(
        EL_API + path,
        headers={"xi-api-key": EL_KEY, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def main() -> int:
    if not EL_KEY:
        print("  ELEVENLABS_API_KEY is not set in .env", file=sys.stderr)
        return 1

    try:
        data = get("/user/subscription")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"  ElevenLabs returned {exc.code}: {body[:300]}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"  Could not reach ElevenLabs: {exc.reason}", file=sys.stderr)
        return 1

    used = data.get("character_count", 0)
    limit = data.get("character_limit", 0)
    remaining = max(limit - used, 0)
    pct = (used / limit * 100) if limit else 0
    reset_unix = data.get("next_character_count_reset_unix")
    reset = (
        datetime.fromtimestamp(reset_unix, tz=timezone.utc).strftime("%Y-%m-%d")
        if reset_unix
        else "unknown"
    )

    print("")
    print("  ElevenLabs usage")
    print(f"  Tier            : {data.get('tier', 'unknown')}")
    print(f"  Characters used : {used:,} / {limit:,} ({pct:.1f}%)")
    print(f"  Remaining       : {remaining:,}")
    print(f"  Resets on       : {reset}")
    if data.get("status"):
        print(f"  Status          : {data['status']}")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

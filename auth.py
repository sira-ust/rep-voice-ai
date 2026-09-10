#!/usr/bin/env python3
"""
Authentication, sessions and rate limiting for the agent web UI.

Standard library only. Used by server.py; also a small CLI for setting up
credentials, because a password hash is easier to generate than to hand-write.

    python auth.py --secret              # generate APP_SECRET
    python auth.py --add-user rep        # prompt for a password, print the entry
    python auth.py --check rep           # verify a password against .env

Design
------
Passwords are stored as scrypt hashes, never plaintext. A successful login sets
an HMAC-signed cookie carrying the username and an expiry; the cookie is
HttpOnly, Secure and SameSite=Strict, which is what stops another site using a
logged-in browser to mint conversation tokens.

Sessions are stateless -- the signature is the only thing that makes them valid,
so there is no store to keep and nothing to clean up. Rotating APP_SECRET
invalidates every session immediately.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# scrypt parameters. n=2**14 with r=8 needs 16 MiB and ~50ms per verification:
# slow enough that guessing is expensive, fast enough for a login form, and
# small enough to stay inside a modest MemoryMax. maxmem must be passed
# explicitly because OpenSSL defaults to a 32 MiB ceiling and refuses anything
# at or above it.
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_MAXMEM = 64 * 1024 * 1024

SESSION_COOKIE = "voiceai_session"


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


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    """scrypt$salt$hash, both base64url, safe to paste into .env."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=SCRYPT_MAXMEM, dklen=32)
    b64 = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
    return "scrypt$%s$%s" % (b64(salt), b64(digest))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, hash_b64 = stored.split("$", 2)
        if scheme != "scrypt":
            return False
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        salt = base64.urlsafe_b64decode(pad(salt_b64))
        expected = base64.urlsafe_b64decode(pad(hash_b64))
    except (ValueError, TypeError):
        return False

    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                            maxmem=SCRYPT_MAXMEM, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


# A built-in account so the app runs straight after a clone, with no setup.
#
# This is a DEMO credential and it is not a secret: the hash lives in a shared
# repository, and the password behind it is short enough that recovering it from
# the hash is trivial. Treat "anyone who can read this repo can sign in" as the
# actual security posture.
#
# Setting APP_USERS in .env replaces this account entirely -- that is how you
# turn the demo account off:
#     python auth.py --add-user rep
DEMO_USER = "testuser"
DEMO_HASH = "scrypt$JB0cogC7bHFgw-s1r0feAQ$-wKATxA0fHfMnQA8-PkNGjdydOeRDEDnzqfVAti57Ao"


def users() -> dict:
    """APP_USERS as "name:hash,name:hash", falling back to the demo account."""
    raw = os.environ.get("APP_USERS", "").strip()
    out = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        name, _, stored = entry.partition(":")
        name = name.strip()
        if name and stored.strip():
            out[name] = stored.strip()
    return out or {DEMO_USER: DEMO_HASH}


def using_demo_account() -> bool:
    """True when no real users are configured, so callers can say so loudly."""
    return not os.environ.get("APP_USERS", "").strip()


def authenticate(username: str, password: str) -> bool:
    """Constant-time-ish: unknown users still pay for a hash computation."""
    table = users()
    stored = table.get(username)
    if stored is None:
        # Verify against a throwaway hash so a missing user takes the same time
        # as a wrong password, instead of returning instantly.
        verify_password(password, hash_password("decoy"))
        return False
    return verify_password(password, stored)


# ------------------------------------------------------------------ sessions

def _secret() -> bytes:
    value = os.environ.get("APP_SECRET", "").strip()
    if value:
        return value.encode("utf-8")
    # No configured secret: use a per-process one. Everything still works, but
    # a restart logs everyone out. server.py warns about this at startup.
    global _EPHEMERAL
    try:
        return _EPHEMERAL
    except NameError:
        _EPHEMERAL = secrets.token_bytes(32)
        return _EPHEMERAL


def session_lifetime() -> int:
    try:
        return max(60, int(os.environ.get("APP_SESSION_HOURS", "12")) * 3600)
    except ValueError:
        return 12 * 3600


def issue_session(username: str) -> str:
    payload = json.dumps({"u": username, "exp": int(time.time()) + session_lifetime()},
                         separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return "%s.%s" % (body, base64.urlsafe_b64encode(sig).decode().rstrip("="))


def read_session(cookie_value: str | None) -> str | None:
    """Return the username if the cookie is validly signed and unexpired."""
    if not cookie_value or "." not in cookie_value:
        return None
    body, _, sig_b64 = cookie_value.rpartition(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    try:
        given = base64.urlsafe_b64decode(pad(sig_b64))
    except (ValueError, TypeError):
        return None

    expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(given, expected):
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(pad(body)))
    except (ValueError, TypeError):
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    name = claims.get("u")
    return name if name in users() else None


# ------------------------------------------------------------------ rate limit

class RateLimiter:
    """Sliding-window cap, in memory.

    Minting a conversation token starts a billable ElevenLabs conversation, so
    an authenticated user should not be able to do it without limit. Per
    process, which is fine for a single-instance deployment.
    """

    def __init__(self, limit: int, window_secs: int = 3600):
        self.limit = limit
        self.window = window_secs
        self._hits = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, int]:
        """Record an attempt. Returns (allowed, remaining)."""
        now = time.time()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False, 0
        bucket.append(now)
        return True, self.limit - len(bucket)


def token_limit() -> int:
    try:
        return max(1, int(os.environ.get("APP_TOKENS_PER_HOUR", "30")))
    except ValueError:
        return 30


# ------------------------------------------------------------------ login page

LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in</title>
<style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:grid;place-items:center;
 background:radial-gradient(900px 480px at 50% -140px,#151a33,transparent 70%) #08090d;
 color:#eceef4;font:15px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
form{background:#101219;border:1px solid #22262f;border-radius:16px;padding:28px;
 width:min(340px,90vw);display:grid;gap:14px;box-shadow:0 10px 40px -20px #000}
h1{margin:0;font-size:17px;font-weight:600;letter-spacing:-.01em}
p{margin:0;font-size:13px;color:#a3abbd}
label{font-size:11px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;color:#6f7789}
input{font:inherit;background:#08090d;border:1px solid #22262f;color:#eceef4;
 border-radius:8px;padding:9px 11px;width:100%;box-sizing:border-box}
input:focus{outline:none;border-color:#6b7cff;box-shadow:0 0 0 3px rgba(107,124,255,.14)}
button{font:inherit;font-weight:560;background:linear-gradient(180deg,#8290ff,#6b7cff);
 color:#fff;border:0;border-radius:999px;padding:11px;cursor:pointer;margin-top:4px}
.err{background:rgba(242,98,111,.09);border:1px solid rgba(242,98,111,.3);
 color:#ffb0b7;border-radius:8px;padding:9px 11px;font-size:13px}
</style>
<form method="post" action="/login">
  <h1>Voice agent</h1>
  <p>Internal tool. Sign in to continue.</p>
  __ERROR__
  <div><label for="u">User</label><input id="u" name="username" autocomplete="username" autofocus required></div>
  <div><label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required></div>
  <button type="submit">Sign in</button>
</form>
"""


def login_page(error: str = "") -> bytes:
    block = '<div class="err">%s</div>' % error if error else ""
    return LOGIN_PAGE.replace("__ERROR__", block).encode("utf-8")


# ------------------------------------------------------------------ cli

def main() -> int:
    load_dotenv(ROOT / ".env")
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0

    if args[0] == "--secret":
        print("\nAdd this to .env (rotating it logs everyone out):\n")
        print("APP_SECRET=%s\n" % secrets.token_urlsafe(48))
        return 0

    if args[0] == "--add-user":
        if len(args) < 2:
            print("  usage: python auth.py --add-user NAME")
            return 1
        name = args[1]
        if ":" in name or "," in name:
            print("  user names cannot contain ':' or ','")
            return 1
        pw = getpass.getpass("  password for %s: " % name)
        again = getpass.getpass("  again: ")
        if pw != again:
            print("  passwords did not match")
            return 1
        if len(pw) < 10:
            print("  use at least 10 characters")
            return 1
        entry = "%s:%s" % (name, hash_password(pw))
        existing = os.environ.get("APP_USERS", "").strip()
        combined = (existing + "," + entry) if existing else entry
        print("\nAdd or replace this line in .env:\n")
        print("APP_USERS=%s\n" % combined)
        return 0

    if args[0] == "--check":
        if len(args) < 2:
            print("  usage: python auth.py --check NAME")
            return 1
        pw = getpass.getpass("  password: ")
        ok = authenticate(args[1], pw)
        print("  %s" % ("OK" if ok else "rejected"))
        return 0 if ok else 1

    print("  unknown option %r" % args[0])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

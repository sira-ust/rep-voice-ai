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

import common  # loads .env on import

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


def users() -> dict:
    """APP_USERS as "name:hash,name:hash".

    No APP_USERS means no accounts, and every login fails. That is deliberate:
    a default account would ship its own hash in the repository, so anyone who
    could read the repository could sign in.
    """
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
    return out


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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap">
<style>
/* PDA 2.0 light theme, using the same token values as public/styles.css --
   including --muted at PDA's #8A97A6, so the sign-in page and the app agree
   exactly rather than this page quietly running its own palette.

   Kept inline and self-contained: this page is served before a session
   exists, and every static route behind the login requires one, so a shared
   stylesheet would either 401 or need its own hole in the auth check. If
   Google Fonts is blocked -- as it is on some mainland China networks -- the
   system stack below takes over and nothing else changes. */
:root{
 color-scheme:light;
 --blue:#2196F3;--blue-d:#1877D2;--ink:#1A2A3A;--muted:#8A97A6;
 --line:#E3E9F0;--bg:#F4F7FB;--strip:#E4E9EF;--red:#D6584F;
 --font:"Nunito",ui-sans-serif,system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",Roboto,sans-serif}
body{margin:0;min-height:100vh;display:grid;place-items:center;
 background:radial-gradient(900px 480px at 50% -140px,#E9EEF6 0%,transparent 70%) var(--bg);
 color:var(--ink);font:15px/1.5 var(--font);-webkit-font-smoothing:antialiased}
form{background:#fff;border:1px solid var(--line);border-radius:18px;padding:28px;
 width:min(340px,90vw);display:grid;gap:14px;box-shadow:0 10px 40px -24px rgba(26,42,58,.45)}
.mark{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;
 background:var(--blue);box-shadow:0 3px 10px -3px rgba(33,150,243,.55);color:#fff;font-weight:800;font-size:15px}
h1{margin:0;font-size:17px;font-weight:800;letter-spacing:-.01em}
p{margin:0;font-size:13px;color:var(--muted)}
label{font-size:11px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
input{font:inherit;background:var(--strip);border:1px solid var(--line);color:var(--ink);
 border-radius:10px;padding:9px 11px;width:100%;box-sizing:border-box;margin-top:5px}
input:focus{outline:none;background:#fff;border-color:var(--blue);box-shadow:0 0 0 3px rgba(33,150,243,.14)}
/* PDA solidbtn: solid fill, soft shadow; pressed is a darkening, never a gradient. */
button{font:inherit;font-weight:800;background:var(--blue);color:#fff;border:0;
 border-radius:999px;padding:12px;cursor:pointer;margin-top:4px;
 box-shadow:0 8px 18px -8px rgba(33,150,243,.55);transition:background .15s,box-shadow .2s,transform .08s}
button:active{background:var(--blue-d);box-shadow:none;transform:scale(.99)}
:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.err{background:rgba(214,88,79,.08);border:1px solid rgba(214,88,79,.28);
 color:#B23A30;border-radius:10px;padding:9px 11px;font-size:13px}
</style>
<form method="post" action="/login">
  <div class="mark" aria-hidden="true">U</div>
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

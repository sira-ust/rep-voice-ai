#!/usr/bin/env python3
"""
Zero-dependency web UI for an ElevenLabs Agent.

Standard library only -- no pip install, no npm. Serves ./public and proxies the
handful of ElevenLabs REST calls that need your API key, so the key stays on the
server and never reaches the browser.

    python server.py            # http://127.0.0.1:8080
    python server.py --port 9000

Configuration lives in .env (see .env.example).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import auth
import common  # loads .env on import

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
API_BASE = "https://api.elevenlabs.io/v1"
UPSTREAM_TIMEOUT = 20

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")



API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()

# Minting a conversation token starts a billable ElevenLabs conversation, so it
# is capped per user even after they have authenticated.
TOKEN_LIMITER = auth.RateLimiter(auth.token_limit())
# Failed logins are capped per source address to make guessing impractical.
LOGIN_LIMITER = auth.RateLimiter(
    int(os.environ.get("APP_LOGIN_ATTEMPTS", "10") or 10), window_secs=900)

# ---- Databricks warehouse pre-warm ---------------------------------------
# A cold serverless warehouse takes 10-15s to answer its first query, which is
# long enough to blow the tool timeout and tell the caller "I encountered an
# error". Starting a conversation is a reliable signal that a lookup is coming,
# so submit a throwaway query then and let the warehouse boot in parallel with
# the greeting.
#
# SELECT 1 touches no table, so the credential used here needs only CAN_USE on
# the warehouse and no catalog grants at all. Use a service principal with
# nothing else granted: the worst an attacker can do with it is start a
# warehouse. Leave DATABRICKS_WARM_TOKEN unset to disable pre-warming entirely.
DBX_HOST = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
DBX_WAREHOUSE = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
DBX_WARM_TOKEN = (os.environ.get("DATABRICKS_WARM_TOKEN", "").strip()
                  or os.environ.get("DATABRICKS_TOKEN", "").strip())
try:
    WARM_EVERY = max(0, int(os.environ.get("DATABRICKS_WARM_MINUTES", "10") or 10)) * 60
except ValueError:
    WARM_EVERY = 600
WARM_READY = bool(DBX_HOST and DBX_WAREHOUSE and DBX_WARM_TOKEN)

_warm_lock = threading.Lock()
_warm_last = 0.0


def warm_warehouse(reason: str) -> None:
    """Nudge the SQL warehouse awake, off the request path.

    Debounced, because several sessions starting together should cost one
    query, and fire-and-forget: wait_timeout=0s makes Databricks return PENDING
    immediately, so nothing here delays the caller's connection.
    """
    global _warm_last
    if not WARM_READY:
        return
    with _warm_lock:
        if time.time() - _warm_last < WARM_EVERY:
            return
        _warm_last = time.time()

    def run() -> None:
        body = json.dumps({
            "warehouse_id": DBX_WAREHOUSE,
            "statement": "SELECT 1",
            "wait_timeout": "0s",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        }).encode("utf-8")
        req = urllib.request.Request(
            DBX_HOST + "/api/2.0/sql/statements/", data=body, method="POST",
            headers={
                "Authorization": "Bearer " + DBX_WARM_TOKEN,
                "Content-Type": "application/json",
                # A CDN in front of the warehouse may reject a default
                # Python-urllib agent, as one did in front of the LLM.
                "User-Agent": "Mozilla/5.0 (compatible) elevenlabs-webui/1.0",
            })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                state = (json.loads(resp.read().decode("utf-8") or "{}")
                         .get("status") or {}).get("state")
            sys.stderr.write("  WARM warehouse %s (%s)\n" % (state, reason))
        except Exception as exc:  # noqa: BLE001 - never affect the request
            sys.stderr.write("  WARM failed: %s (%s)\n" % (exc, reason))

    threading.Thread(target=run, daemon=True, name="dbx-warm").start()


# The rep list for the picker. Cached, because it changes about as often as
# somebody joins the company and every sign-in would otherwise wake the
# warehouse. Uses the warm-up token, which is the only Databricks credential
# this server holds and is read-only; the agent's own lookups go from
# ElevenLabs straight to Databricks and never through here.
_reps_cache: list = []
_reps_fetched = 0.0
_reps_lock = threading.Lock()
REPS_TTL = 15 * 60


def sales_reps() -> list:
    """Active reps who own accounts, newest list at most REPS_TTL old."""
    global _reps_cache, _reps_fetched
    if not WARM_READY:
        return []
    with _reps_lock:
        if _reps_cache and time.time() - _reps_fetched < REPS_TTL:
            return _reps_cache
    body = json.dumps({
        "warehouse_id": DBX_WAREHOUSE,
        "statement": ("SELECT sales_code, rep_name FROM ust_databricks.ust_dims.dim_reps "
                      "WHERE is_active AND owns_accounts AND rep_name IS NOT NULL "
                      "ORDER BY rep_name"),
        "wait_timeout": "30s",
        "on_wait_timeout": "CANCEL",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    }).encode("utf-8")
    req = urllib.request.Request(
        DBX_HOST + "/api/2.0/sql/statements/", data=body, method="POST",
        headers={"Authorization": "Bearer " + DBX_WARM_TOKEN,
                 "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible) elevenlabs-webui/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        rows = ((data.get("result") or {}).get("data_array")) or []
        reps = [{"code": r[0], "name": r[1]} for r in rows if len(r) > 1 and r[1]]
    except Exception as exc:  # noqa: BLE001 - the picker degrades to "All"
        sys.stderr.write("  REPS lookup failed: %s\n" % exc)
        return _reps_cache
    with _reps_lock:
        _reps_cache, _reps_fetched = reps, time.time()
    return reps


CSP = os.environ.get("APP_CSP", "on").strip().lower() not in ("off", "0", "false")

# Behind a reverse proxy, X-Forwarded-For is the real client. Off by default:
# trusting that header when nothing sets it lets a caller forge their own IP
# and walk straight past the login rate limit.
TRUST_PROXY = os.environ.get("APP_TRUST_PROXY", "").strip().lower() in ("1", "true", "on")

# Session cookies are Secure, so a browser will not send them back over plain
# HTTP -- which makes a loopback login silently fail to stick. Relaxed
# automatically when bound to loopback (main() does this); anywhere else it
# stays on unless APP_INSECURE_COOKIE says otherwise.
_INSECURE_ENV = os.environ.get("APP_INSECURE_COOKIE", "").strip().lower()
INSECURE_COOKIE = _INSECURE_ENV in ("1", "true", "on")
_INSECURE_SET_EXPLICITLY = _INSECURE_ENV != ""

# The browser loads the SDK from jsDelivr and talks to ElevenLabs directly, so
# both have to be allowed. blob: is required: the SDK builds its AudioWorklet
# from a blob URL, and revoking that permission silently kills the microphone.
#
# styles.css @imports Nunito from Google Fonts, which needs the stylesheet host
# in style-src and the font files in font-src -- font-src is not inherited from
# style-src, and without it the CSS loads while every glyph is blocked. Neither
# failure shows up as anything but the wrong typeface.
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' blob: https://cdn.jsdelivr.net; "
    "worker-src 'self' blob:; "
    "child-src 'self' blob:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data:; "
    "media-src 'self' blob: mediastream:; "
    "connect-src 'self' https://cdn.jsdelivr.net https://api.elevenlabs.io "
    "wss://api.elevenlabs.io https://*.elevenlabs.io wss://*.elevenlabs.io; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


TOOLS_FILE = ROOT / "databricks_tools.json"


def load_tools_file() -> dict:
    """databricks_tools.json, for the in-app guide. A missing or broken file is
    not fatal -- the agent still works, the UI just shows no guide."""
    if not TOOLS_FILE.is_file():
        return {}
    try:
        raw = json.loads(TOOLS_FILE.read_text(encoding="utf-8-sig"))
    except ValueError:
        return {}
    if isinstance(raw, list):
        return {"tools": raw, "tables": {}}
    return raw if isinstance(raw, dict) else {}


class UpstreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def call_elevenlabs(path: str, params: dict | None = None) -> dict:
    """GET the ElevenLabs API with the server-side key attached."""
    if not API_KEY:
        raise UpstreamError(
            500,
            "ELEVENLABS_API_KEY is not set. Put it in the .env file next to "
            "server.py, then restart the server.",
        )

    url = API_BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    req = urllib.request.Request(
        url,
        headers={
            "xi-api-key": API_KEY,
            "Accept": "application/json",
            "User-Agent": "elevenlabs-webui/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        detail = body
        try:
            parsed = json.loads(body)
            detail = parsed.get("detail", parsed)
            if isinstance(detail, dict):
                detail = detail.get("message") or json.dumps(detail)
        except (ValueError, AttributeError):
            pass
        if exc.code == 401:
            detail = "ElevenLabs rejected the API key (401). " + str(detail)
        raise UpstreamError(exc.code, str(detail)[:500]) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(502, "Could not reach the ElevenLabs API: %s" % (exc.reason,)) from exc


class Handler(BaseHTTPRequestHandler):
    server_version = "ElevenLabsWebUI/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- plumbing ----------

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str,
              extra: list | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
        if CSP:
            self.send_header("Content-Security-Policy", CSP_POLICY)
        for name, value in (extra or []):
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self._send(status, raw, "application/json; charset=utf-8")

    # ---------- identity ----------

    def _client(self) -> str:
        """Source address, honouring one proxy hop if configured."""
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _user(self) -> str | None:
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            jar = http.cookies.SimpleCookie()
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(auth.SESSION_COOKIE)
        return auth.read_session(morsel.value) if morsel else None

    def _audit(self, action: str, detail: str = "") -> None:
        sys.stderr.write("  AUDIT user=%s ip=%s %s %s\n" % (
            self._user() or "-", self._client(), action, detail))

    def _redirect(self, location: str, extra: list | None = None) -> None:
        self._send(303, b"", "text/plain; charset=utf-8",
                   [("Location", location)] + (extra or []))

    # ---------- routing ----------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if route == "/login":
            if self._user():
                self._redirect("/")
            else:
                self._send(200, auth.login_page(), "text/html; charset=utf-8")
            return

        if route == "/logout":
            self._audit("logout")
            self._redirect("/login", [(
                "Set-Cookie",
                "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % auth.SESSION_COOKIE)])
            return

        # Everything past here needs a session.
        if not self._user():
            if route.startswith("/api"):
                self._json(401, {"error": "Not signed in.", "login": "/login"})
            else:
                self._redirect("/login")
            return

        if route.startswith("/api"):
            try:
                self._handle_api(route, query)
            except UpstreamError as exc:
                status = exc.status if 400 <= exc.status < 600 else 502
                self._json(status, {"error": exc.message})
            except Exception as exc:  # noqa: BLE001 - never kill the dev server
                self._json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return

        self._serve_static(parsed.path)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if route != "/login":
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return

        allowed, _ = LOGIN_LIMITER.check("login:" + self._client())
        if not allowed:
            self._audit("login_throttled")
            self._send(429, auth.login_page("Too many attempts. Wait 15 minutes."),
                       "text/html; charset=utf-8")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            self._send(400, auth.login_page("Bad request."), "text/html; charset=utf-8")
            return

        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        username = (form.get("username", [""])[0] or "").strip()
        password = form.get("password", [""])[0] or ""

        if not auth.authenticate(username, password):
            sys.stderr.write("  AUDIT user=%s ip=%s login_failed\n"
                             % (username or "-", self._client()))
            self._send(401, auth.login_page("Wrong user name or password."),
                       "text/html; charset=utf-8")
            return

        sys.stderr.write("  AUDIT user=%s ip=%s login_ok\n" % (username, self._client()))
        # Signing in usually precedes a call by seconds, so start the warehouse
        # now for the extra head start. Debounced, so this costs nothing when
        # the session-start warm-up has already run.
        warm_warehouse("login")
        cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (
            auth.SESSION_COOKIE, auth.issue_session(username),
            auth.session_lifetime(), "" if INSECURE_COOKIE else "; Secure")
        self._redirect("/", [("Set-Cookie", cookie)])

    def _handle_api(self, route: str, query: dict) -> None:
        agent_id = (query.get("agent_id", [AGENT_ID])[0] or AGENT_ID).strip()

        if route == "/api/config":
            # Deliberately no fragment of the API key: eight characters of a
            # credential is eight characters an attacker does not have to guess.
            self._json(200, {
                "agentId": AGENT_ID,
                "hasApiKey": bool(API_KEY),
                "user": self._user(),
            })
            return

        if route == "/api/reps":
            # Who the caller can claim to be, until real sign-in exists.
            self._json(200, {"reps": sales_reps()})
            return

        if route == "/api/capabilities":
            # Built from databricks_tools.json -- the same file that defines the
            # tools -- so the guide cannot drift from what the agent can
            # actually do. Only table names and sample questions are exposed;
            # no SQL, no credentials.
            config = load_tools_file()
            descriptions = config.get("tables") or {}
            groups = {}
            for spec in (config.get("tools") or []):
                subject = spec.get("subject") or "Data"
                entry = groups.setdefault(subject, {"subject": subject, "tables": [],
                                                    "questions": [], "answers": []})
                full = spec.get("table") or ""
                if full and not any(t["name"] == full.split(".")[-1]
                                    for t in entry["tables"]):
                    meta = descriptions.get(full) or {}
                    entry["tables"].append({
                        "name": full.split(".")[-1],
                        "label": meta.get("label") or "",
                        "about": meta.get("about") or "",
                        "grain": meta.get("grain") or "",
                        "notCovered": meta.get("not_covered") or "",
                    })
                for question in (spec.get("sample_questions") or []):
                    if question not in entry["questions"]:
                        entry["questions"].append(question)
                # One worked example per subject. Knowing what an answer sounds
                # like is most of knowing whether to ask -- a caller who expects
                # a spreadsheet and hears two sentences assumes it failed.
                answer = spec.get("answer_example")
                if answer and answer not in entry["answers"]:
                    entry["answers"].append(answer)
            # What the agent cannot answer yet, and what each is waiting on.
            # Shown so a rep learns the limit from the page rather than from a
            # question that fails halfway through a call.
            tbd = []
            for entry in ((config.get("tbd") or {}).get("waiting_on") or []):
                for question in (entry.get("unlocks") or []):
                    tbd.append({"question": question, "waitingOn": entry.get("table") or ""})
            self._json(200, {"groups": [g for g in groups.values() if g["questions"]],
                             "tbd": tbd})
            return

        if route == "/api/agents":
            data = call_elevenlabs("/convai/agents", {"page_size": "100"})
            agents = [
                {"agentId": a.get("agent_id"), "name": a.get("name") or a.get("agent_id")}
                for a in data.get("agents", [])
                if a.get("agent_id")
            ]
            self._json(200, {"agents": agents})
            return

        if route == "/api/agent-info":
            if not agent_id:
                raise UpstreamError(400, "No agent selected.")
            data = call_elevenlabs("/convai/agents/" + urllib.parse.quote(agent_id))
            prompt = data.get("conversation_config", {}).get("agent", {}).get("prompt") or {}
            custom = prompt.get("custom_llm") or {}
            self._json(200, {
                "name": data.get("name"),
                "llm": prompt.get("llm"),
                "customLlmUrl": custom.get("url"),
                "customLlmModel": custom.get("model_id"),
            })
            return

        if route in ("/api/conversation-token", "/api/signed-url"):
            user = self._user() or "-"
            allowed, remaining = TOKEN_LIMITER.check("tok:" + user)
            if not allowed:
                self._audit("token_throttled", route)
                raise UpstreamError(
                    429, "Conversation limit reached (%d per hour). Try again later."
                    % auth.token_limit())
            self._audit("mint", "%s agent=%s remaining=%d" % (route, agent_id, remaining))
            # The caller is seconds away from a lookup; boot the warehouse now.
            warm_warehouse("session start")

        if route == "/api/conversation-token":
            if not agent_id:
                raise UpstreamError(400, "No agent selected. Set ELEVENLABS_AGENT_ID in .env or pick one in the UI.")
            data = call_elevenlabs("/convai/conversation/token", {"agent_id": agent_id})
            token = data.get("token")
            if not token:
                raise UpstreamError(502, "ElevenLabs did not return a conversation token.")
            self._json(200, {"token": token, "conversationId": data.get("conversation_id")})
            return

        if route == "/api/signed-url":
            if not agent_id:
                raise UpstreamError(400, "No agent selected. Set ELEVENLABS_AGENT_ID in .env or pick one in the UI.")
            data = call_elevenlabs("/convai/conversation/get-signed-url", {"agent_id": agent_id})
            signed_url = data.get("signed_url")
            if not signed_url:
                raise UpstreamError(502, "ElevenLabs did not return a signed URL.")
            self._json(200, {"signedUrl": signed_url})
            return

        self._json(404, {"error": "Unknown API route: " + route})

    def _serve_static(self, url_path: str) -> None:
        rel = urllib.parse.unquote(url_path).lstrip("/") or "index.html"
        base = PUBLIC.resolve()
        target = (base / rel).resolve()

        # Refuse anything that escapes ./public
        if base != target and base not in target.parents:
            self._send(403, b"Forbidden", "text/plain; charset=utf-8")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the ElevenLabs agent web UI.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    args = parser.parse_args()

    url = "http://%s:%d" % (args.host, args.port)
    key_state = "loaded from .env" if API_KEY else "MISSING -- add ELEVENLABS_API_KEY to .env"
    print("")
    print("  ElevenLabs Agent Web UI")
    print("  " + url)
    print("  API key : " + key_state)
    print("  Agent   : " + (AGENT_ID or "not set -- pick one in the UI"))
    print("  Users   : " + ", ".join(sorted(auth.users())))
    print("  Limits  : %d conversations/user/hour" % auth.token_limit())
    print("  CSP     : " + ("on" if CSP else "off"))
    print("  Warm-up : " + ("warehouse %s, at most every %d min"
                            % (DBX_WAREHOUSE, WARM_EVERY // 60) if WARM_READY
                            else "off (set DATABRICKS_WARM_TOKEN to enable)"))
    if not auth.users():
        print("")
        print("  NO ACCOUNTS CONFIGURED, so every login will fail. Add one:")
        print("    python auth.py --add-user rep   -> APP_USERS=... in .env")
        print("")
    if not os.environ.get("APP_SECRET", "").strip():
        print("  WARNING : APP_SECRET is unset, so sessions die on restart.")
        print("            python auth.py --secret")
    global INSECURE_COOKIE
    if not _INSECURE_SET_EXPLICITLY and args.host in ("127.0.0.1", "::1", "localhost"):
        # Loopback is not network-reachable, so dropping Secure here costs
        # nothing and is the difference between the login working and not.
        INSECURE_COOKIE = True
        print("  Cookies : Secure relaxed for loopback (plain HTTP works)")
    else:
        print("  Cookies : Secure%s" % ("" if not INSECURE_COOKIE else " DISABLED by APP_INSECURE_COOKIE"))
    print("  Ctrl+C to stop")
    print("")

    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print("  Could not bind %s:%d -- %s" % (args.host, args.port, exc), file=sys.stderr)
        print("  Try: python server.py --port 8081", file=sys.stderr)
        return 1

    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

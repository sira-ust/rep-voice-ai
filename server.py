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
import hmac
import json
import mimetypes
import os
import re
import secrets
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


# The page waits for this before it lets a call start, so a rep's first
# question is not the one that sits through a cold warehouse. A query that
# answered in the last READY_FOR seconds counts as proof it is awake.
READY_FOR = 5 * 60
_ready_at = 0.0
_ready_lock = threading.Lock()


def mark_warehouse_ready() -> None:
    global _ready_at
    with _ready_lock:
        _ready_at = time.time()


def wait_for_warehouse(timeout: int = 50) -> tuple[bool, str]:
    """Block until the warehouse answers SELECT 1, or `timeout` seconds pass.

    Unlike warm_warehouse, which only nudges, this waits: wait_timeout makes
    Databricks hold the request until the statement finishes, which on a cold
    warehouse is the start-up time itself.
    """
    if not WARM_READY:
        return True, "no warehouse configured"
    with _ready_lock:
        if time.time() - _ready_at < READY_FOR:
            return True, "recently answered"
    body = json.dumps({
        "warehouse_id": DBX_WAREHOUSE,
        "statement": "SELECT 1",
        "wait_timeout": "%ds" % max(5, min(50, timeout)),
        "on_wait_timeout": "CONTINUE",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    }).encode("utf-8")
    req = urllib.request.Request(
        DBX_HOST + "/api/2.0/sql/statements/", data=body, method="POST",
        headers={"Authorization": "Bearer " + DBX_WARM_TOKEN,
                 "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible) elevenlabs-webui/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout + 10) as resp:
            state = ((json.loads(resp.read().decode("utf-8") or "{}").get("status") or {})
                     .get("state"))
    except Exception as exc:  # noqa: BLE001 - the page decides what to do
        return False, str(exc)
    if state == "SUCCEEDED":
        mark_warehouse_ready()
        return True, "awake"
    return False, "warehouse still starting (%s)" % state


# ---- LLM proxy: the only place Databricks is ever reached from a live call --
# ElevenLabs POSTs here (CUSTOM_LLM_URL) instead of straight to the real
# Gemma/vLLM host. We run the model, execute any tool call it asks for
# ourselves using DATABRICKS_TOKEN (never given to ElevenLabs), and hand back
# only the final plain-text answer -- ElevenLabs never sees a tool exists.
LLM_UPSTREAM_URL = os.environ.get("LLM_UPSTREAM_URL", "").strip().rstrip("/")
LLM_UPSTREAM_KEY = os.environ.get("LLM_UPSTREAM_API_KEY", "").strip()
# A WAF/CDN in front of the LLM host (Cloudflare, for testai.qskyabc.com) can
# 403 a non-browser User-Agent, which otherwise surfaces as a generic
# "trouble finding that" failure indistinguishable from a real model error.
LLM_UPSTREAM_USER_AGENT = os.environ.get("LLM_UPSTREAM_USER_AGENT", "").strip()
LLM_PROXY_SECRET = os.environ.get("LLM_PROXY_SECRET", "").strip()
try:
    LLM_PROXY_MAX_ROUNDS = max(1, int(os.environ.get("LLM_PROXY_MAX_TOOL_ROUNDS", "4") or 4))
except ValueError:
    LLM_PROXY_MAX_ROUNDS = 4
# How many times to ask again when the model returns an empty turn. Two is
# enough: the failure is intermittent rather than sticky, and every retry is
# another few seconds of a caller waiting.
try:
    EMPTY_RETRIES = max(0, int(os.environ.get("LLM_PROXY_EMPTY_RETRIES", "2") or 2))
except ValueError:
    EMPTY_RETRIES = 2
LLM_PROXY_READY = bool(LLM_UPSTREAM_URL and LLM_PROXY_SECRET)

_tool_specs_cache: list[dict] | None = None


# ------------------------------------------------------------------ live transcript
#
# ElevenLabs sends the page the rep's own words only together with the first
# sentence of the reply, so a ten-second lookup kept them off the screen for
# ten seconds. The proxy has those words half a second after the rep stops
# talking, and it runs in this same process as the page's own server -- so it
# hands them over directly. The page opens /api/live for its call and shows
# them the moment they land; ElevenLabs' copy arrives later as a duplicate.
#
# In memory and per process, like the rate limits: a restart loses a live
# call anyway.

LIVE_CALL_SECS = 4 * 3600      # matches the scope token's lifetime
_live_lock = threading.Condition()
_live_calls: dict[str, dict] = {}


def live_open(user: str) -> str:
    """A new call id, readable only by `user`."""
    call = secrets.token_urlsafe(16)
    now = time.time()
    with _live_lock:
        for key in [k for k, v in _live_calls.items() if v["expires"] < now]:
            del _live_calls[key]
        _live_calls[call] = {"user": user, "expires": now + LIVE_CALL_SECS,
                             "events": [], "last": "", "seq": 0}
    return call


def live_publish_user_turn(call: str, text: str) -> int:
    """The rep's latest words, for the page that owns `call`.

    ElevenLabs often asks more than once for one turn: at each pause, and
    again when the rep carries on, sometimes re-transcribing the whole thing.
    An exact repeat is dropped here; the page keeps one bubble per turn and
    lets each later version replace the one before.

    Returns this request's number. ElevenLabs asks at every pause while the rep
    is still talking and abandons all but the last request, without always
    closing the others -- so the newest request for a call is the only one
    whose reply is real (see live_is_current).
    """
    text = (text or "").strip()
    with _live_lock:
        entry = _live_calls.get(call) if call else None
        if not entry:
            return 0
        entry["seq"] += 1
        seq = entry["seq"]
        if not text or text == entry["last"]:
            return seq
        entry["last"] = text
        entry["events"].append({"type": "user", "text": text})
        _live_lock.notify_all()
        return seq


EVIDENCE_TURNS = 6   # earlier turns whose lookups are carried forward

# A lookup still running this long after the last thing said gets another
# short line, so ElevenLabs (which waits about 15 s) does not drop the turn.
try:
    KEEPALIVE_SECS = max(3.0, float(os.environ.get("LLM_PROXY_KEEPALIVE_SECS", "8") or 8))
except ValueError:
    KEEPALIVE_SECS = 8.0
# Enough to carry a slow turn past ~45 s. When these ran out, ElevenLabs gave
# up, asked again, and the new request started the lookup from scratch.
KEEPALIVE_LINES = ("Still checking.", "Almost there.", "Still working on it.", "Just a moment more.")


def _flat(text: str) -> str:
    return " ".join((text or "").split())


def live_remember(call: str, spoken: str, exchange: list) -> None:
    """Keep a turn's lookups, keyed by the reply they produced.

    ElevenLabs sends the proxy the conversation as text alone: the rep's words
    and the agent's earlier replies, never the lookups behind them, which ran
    here. So on the next turn the model sees its own earlier sentence listing
    three items with nothing to say which account they belonged to. Asked
    "yes" to looking at a second store's pitch list, it recited the first
    store's items as the second's, without looking anything up.
    with_evidence puts these back where they happened.
    """
    if not call or not exchange or not spoken:
        return
    with _live_lock:
        entry = _live_calls.get(call)
        if entry is not None:
            kept = entry.setdefault("evidence", [])
            kept.append({"key": _flat(spoken)[:80], "messages": exchange})
            del kept[:-EVIDENCE_TURNS]


def with_evidence(call: str, messages: list) -> list:
    """The conversation with each earlier turn's lookups back before its reply.

    A reply is recognised by its opening words, which is how ElevenLabs quotes
    it back -- whole, or cut short where the rep interrupted.
    """
    with _live_lock:
        entry = _live_calls.get(call) if call else None
        kept = list((entry or {}).get("evidence") or [])
    if not kept:
        return messages
    out = []
    stored = len(kept)
    for message in messages:
        if message.get("role") == "assistant" and isinstance(message.get("content"), str):
            said = _flat(message["content"])
            for item in kept:
                if item["key"] and item["key"][:40] in said:
                    out.extend(item["messages"])
                    kept.remove(item)
                    break
        out.append(message)
    # A turn's lookups that find no reply to sit beside are lost to the model,
    # which then answers follow-ups from memory -- the one failure here that
    # produces confident wrong numbers.
    if kept:
        sys.stderr.write("  EVIDENCE %d of %d earlier turn(s) not matched: %s\n"
                         % (len(kept), stored, " | ".join(repr(i["key"][:40]) for i in kept)))
    return out


def live_is_current(call: str, seq: int) -> bool:
    """False once a newer request for the same call has arrived."""
    with _live_lock:
        entry = _live_calls.get(call) if call else None
        return not entry or entry["seq"] == seq


def live_publish_visual(call: str, visual: dict, seq: int = 0) -> None:
    """A chart, table or map for the page, under the reply it belongs to."""
    if not call or not visual:
        return
    with _live_lock:
        entry = _live_calls.get(call)
        if entry and (not seq or entry["seq"] == seq):
            entry["events"].append({"type": "visual", "visual": visual})
            _live_lock.notify_all()


def live_publish_reply(call: str, text: str, start: bool, seq: int = 0) -> None:
    """A piece of the proxy's own reply, the moment it goes to ElevenLabs.

    ElevenLabs streams reply text to the page too, but holds it back -- the
    "Let me check." spoken at once reached the page only with the full answer,
    seconds later. `start` opens a new bubble; later pieces append to it.
    """
    if not call or not text:
        return
    with _live_lock:
        entry = _live_calls.get(call)
        if entry and (not seq or entry["seq"] == seq):
            entry["events"].append({"type": "agent", "text": text, "start": start})
            _live_lock.notify_all()


# ------------------------------------------------------------------ screen and voice
#
# One reply serves two readers. The page shows it and ElevenLabs speaks it, and
# a paragraph of spelled-out figures is hard to scan. So the model writes for
# the screen -- digits, currency, light markdown -- the page shows exactly that,
# and ElevenLabs gets the same words with the markdown taken out. Figures are
# left as digits for ElevenLabs to read (text_normalisation_type "elevenlabs"
# on the agent), which says "$4,210" as a person would.
#
# Proxy mode only, so it is added here rather than written into agent_prompt.md:
# in direct mode ElevenLabs speaks the model's text as it is, markdown and all.

DISPLAY_STYLE = """

## How to write your replies (this overrides earlier guidance on numbers)

Your reply is shown on the rep's screen as well as spoken. The voice is made
from the same text separately, so write it to be read:

- Numbers as digits, never words: 189 units, 42%, 3 orders.
- Money with the dollar sign and thousands separators: $4,210, $12.50. Every
  figure is US dollars. In Chinese, write 4,210美元 rather than using $.
- Dates short: Sep 22, not the twenty-second of September.
- Names in ordinary capitals -- New Asian Supermarket Inc, West Allis, Eel
  W/Teriyaki Glaze -- not the all-capitals the system stores them in.
- **Bold** account names, item names and the key figure in an answer.
- *Italics* for a caveat or a hedge, such as *as of Sep 22*.
- ==Highlight== the one thing the rep must not miss, at most once per reply.
- Three or more items: a short list, one per line, each starting with "- ".
- Nothing else: no headings, tables, links, code or emoji -- apart from the
  visual block below, and a visit plan's plan block.

Keep it as short as you would say it. The formatting helps the eye; it is not
a reason to write more.

## Where facts come from

Every item, figure, date or status you give for an account must come from a
lookup result for that same account -- in this turn, or shown earlier in this
conversation as a tool result. Your own earlier replies are not a source: they
are what you said, not what the data says.

- A different account from the last one means a new lookup for it. Never
  reuse one store's items, numbers or status for another.
- When the rep says yes to something you offered to look up, look it up.
- If there is no result for what they asked, say you will check, and check.

## Showing a picture on the rep's screen

Under your reply the screen can show one chart, table or map, built from a
lookup's rows.

Most lookup results come with show_on_screen: a ready block for those rows.
After your reply, copy it exactly as given -- unless the answer is a single
number or a question back to the rep, or a different picture below fits what
they asked better. With lookups from more than one tool, use the block of the
one your answer is mostly about.

To write your own, pick the kind that fits:

- map: where accounts are -- rows from lookup_accounts_in_city,
  lookup_accounts_on_route, lookup_accounts_to_call or lookup_winback_accounts.
  Always for a trip.
- bar: comparing an amount across a few rows (biggest orders, how far past
  their usual gap), or across a few columns of one row.
- pie: only for parts that together make one whole. Never for shares that
  overlap or do not add up -- promo, new and returned lines are separate
  rates, so they are a bar.
- line: one amount over dates.
- table: more than three rows the rep will want to scan, such as a pitch list
  or top items.

None for a single number, a yes or no, or a question back to the rep.

Write it after your reply, on lines of its own, exactly in this form:

```visual
{"type": "bar", "from": "lookup_biggest_orders", "title": "Biggest orders this month", "label": "customer_name", "values": ["grand_total"]}
```

- "from": the lookup whose rows to draw, one already run in this conversation.
- "label", "values" and "columns" are column names exactly as they appear in
  those rows. Never write numbers or names yourself: the screen fills them in
  from the lookup, so the picture always matches the data.
- "where" (optional) keeps only rows where a column has a value:
  {"period_type": "month"}.
- table: "columns", at most 6. map: nothing beyond "from" and "title".

The block is not spoken. Never read it out or describe it; you may say
"I've put them on a map" or "they're on your screen".

## Visit plans

When lookup_visit_plan has run this turn, the plan goes to the rep's screen by
itself, built from your plan block: your angles first, every other idea below
them. Write no visual block. Your reply is the spoken summary of your angles,
briefly, followed by the plan block its before_you_answer describes.
"""


def with_display_style(messages: list) -> list:
    """The conversation with DISPLAY_STYLE added to its system prompt.

    Appended to the existing system message rather than sent as a second one:
    chat templates differ on whether a second system message is allowed, and
    a rejected request is a dead turn.
    """
    out = [dict(m) for m in messages]
    for message in out:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] += DISPLAY_STYLE
            return out
    return [{"role": "system", "content": DISPLAY_STYLE.strip()}] + out


_MD_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_MD_MARKS = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"__(.+?)__"), r"\1"),
    (re.compile(r"==(.+?)=="), r"\1"),
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),
    (re.compile(r"(?<![\w])_(?!\s)(.+?)(?<!\s)_(?![\w])"), r"\1"),
]


def speakable(text: str) -> str:
    """The same reply without markdown, for the voice.

    List items become sentences: a line break is not a pause to a voice, so an
    item without closing punctuation gets a full stop, or three items run
    together into one breathless phrase.
    """
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        bullet = bool(_MD_BULLET.match(line))
        line = _MD_BULLET.sub("", line)
        for pattern, repl in _MD_MARKS:
            line = pattern.sub(repl, line)
        line = line.replace("**", "").replace("==", "")
        if bullet and line and line[-1] not in ".!?:;,":
            line += "."
        lines.append(line)
    return " ".join(lines)


# ------------------------------------------------------------------ visuals
#
# The model chooses a picture -- which lookup, which columns, what kind -- and
# writes it as a small block after its reply. It never writes the figures: the
# values come from the lookup's own rows, here. A chart is read as the data
# itself, so a miscopied digit in one would be believed more than one in a
# sentence, and miscopied digits are this model's best-known failure.
#
# The block is taken out before anything is spoken or shown as text. One that
# cannot be built is dropped and logged; the spoken answer never depends on it.

VISUAL_TYPES = ("bar", "pie", "line", "table", "map")
VISUAL_MAX_ROWS = {"bar": 12, "pie": 8, "line": 31, "table": 25, "map": 25}
_VISUAL_FENCE = re.compile(r"```[ \t]*(\w*)[ \t]*\n?(.*?)```", re.S)
_VISUAL_BARE = re.compile(r'^[ \t]*\{[ \t]*"type"[ \t]*:.*\}[ \t]*$', re.M)
# Columns the screen has no use for as values or table cells.
_VISUAL_SKIP = {"in_words", "latitude", "longitude", "from_lat", "from_lon", "to_lat", "to_lon",
                "customer_key", "item_no", "owner_rep_name", "rep_name"}


def column_format(name: str) -> str:
    """How a column's numbers read: money, a percentage, a multiple, or plain."""
    n = name.lower()
    if "pct" in n or "share" in n or n.endswith("_rate"):
        return "percent"         # stored as fractions: 0.152 is 15.2%
    if n == "days_past_usual":
        return "times"
    if any(w in n for w in ("sales", "amount", "value", "total", "target", "profit",
                            "price", "needed", "projected", "revenue")) and "count" not in n:
        return "money"
    return "number"


def _visual_rows(history: list, source: str, needs: list) -> tuple[str, list]:
    """The newest rows from lookup `source` -- or, if the model named it
    wrongly, from the newest lookup that has every column it asked for."""
    names = {}
    for message in history:
        for call in message.get("tool_calls") or []:
            names[call.get("id")] = (call.get("function") or {}).get("name") or ""
    fallback = ("", [])
    for message in reversed(history):
        if message.get("role") != "tool":
            continue
        try:
            rows = json.loads(message.get("content") or "{}").get("rows") or []
        except (ValueError, AttributeError):
            continue
        if not rows:
            continue
        name = names.get(message.get("tool_call_id"), "")
        if name == source:
            return name, rows
        if not fallback[1] and all(col in rows[0] for col in needs):
            fallback = (name, rows)
    return fallback


def _title_case(value) -> str:
    text = "" if value is None else str(value)
    if text.lower() in ("true", "false"):
        return "Yes" if text.lower() == "true" else "No"
    if text.isupper():
        text = re.sub(r"\bLlc\b", "LLC", text.title())
    return text


def build_visual(spec: dict, history: list) -> tuple[dict | None, str]:
    """The model's block, filled in from the lookup it names. (visual, why-not)."""
    kind = str(spec.get("type") or "").lower()
    if kind not in VISUAL_TYPES:
        return None, "unknown type %r" % kind
    label = spec.get("label") or ""
    values = spec.get("values") or ([spec["value"]] if spec.get("value") else [])
    values = [v for v in values if isinstance(v, str)][:4]
    columns = [c for c in (spec.get("columns") or []) if isinstance(c, str)][:6]
    needs = ([label] if label else []) + values + columns
    if kind == "map":
        needs = ["latitude", "longitude"]
    source, rows = _visual_rows(history, str(spec.get("from") or ""), needs)
    if not rows:
        return None, "no lookup rows for %r" % spec.get("from")

    where = spec.get("where") if isinstance(spec.get("where"), dict) else {}
    for col, want in where.items():
        rows = [r for r in rows if str(r.get(col, "")).lower() == str(want).lower()]
    rows = rows[:VISUAL_MAX_ROWS[kind]]
    if not rows:
        return None, "no rows left after where=%r" % where
    title = " ".join(str(spec.get("title") or "").split())[:80]
    out = {"type": kind, "title": title, "source": source}

    if kind == "map":
        points = []
        for r in rows:
            lat, lon = _num(r.get("latitude")), _num(r.get("longitude"))
            if lat is None or lon is None:
                continue
            points.append({"label": _title_case(r.get("customer_name") or r.get(label)),
                           "lat": lat, "lon": lon,
                           "status": r.get("rhythm_status") or "",
                           "detail": r.get("in_words") or ""})
        if not points:
            return None, "no rows with coordinates"
        out["points"] = points
        first = rows[0]
        if _num(first.get("from_lat")) is not None and _num(first.get("to_lat")) is not None:
            out["route"] = {
                "from": {"label": first.get("from_place") or "", "lat": _num(first["from_lat"]),
                         "lon": _num(first["from_lon"])},
                "to": {"label": first.get("to_place") or "", "lat": _num(first["to_lat"]),
                       "lon": _num(first["to_lon"])},
            }
            # Cached from the lookup a moment ago, so no second request.
            road = road_route(first["from_lat"], first["from_lon"], first["to_lat"], first["to_lon"])
            if road:
                out["route"].update(road)
        return out, ""

    if kind == "table":
        cols = [c for c in columns if c in rows[0]] or \
               [c for c in rows[0] if c not in _VISUAL_SKIP][:6]
        out["columns"] = [{"name": c, "format": column_format(c)} for c in cols]
        # Numbers as numbers, for the page to format; everything else as text.
        out["rows"] = [[_num_or_none(r.get(c)) if _num_or_none(r.get(c)) is not None
                        else _title_case(r.get(c)) for c in cols] for r in rows]
        return out, ""

    # bar, pie, line
    values = [v for v in values if v in rows[0]]
    if not values:
        return None, "none of the value columns %r are in %s" % (spec.get("values"), source)
    if len(rows) == 1 and len(values) > 1:
        # One row, several columns: the columns are the categories, under the
        # names the block gives them ("names") or their own, made readable.
        names = spec.get("names") if isinstance(spec.get("names"), dict) else {}
        if not names:
            # The model tends to drop "names" when it copies a block; the
            # lookup's own suggestion still has them.
            try:
                names = (common.find_spec(tool_specs(), source).get("visual") or {}).get("names") or {}
            except common.Fail:
                names = {}
        labels = [str(names.get(v) or v.replace("_", " ").capitalize())[:40] for v in values]
        series = [{"name": title or "", "format": column_format(values[0]),
                   "values": [_num_or_none(rows[0].get(v)) for v in values]}]
    else:
        if label not in rows[0]:
            return None, "label column %r is not in %s" % (label, source)
        labels = [_title_case(r.get(label)) for r in rows]
        series = [{"name": v.replace("_", " ").capitalize(), "format": column_format(v),
                   "values": [_num_or_none(r.get(v)) for r in rows]} for v in values]
    for s, v in zip(series, values):
        # Progress toward a target gets its 100% line; other rates do not --
        # a 5% share drawn against 100% is a sliver.
        s["target"] = "target" in v and s["format"] == "percent"
    if all(x is None for s in series for x in s["values"]):
        return None, "value columns hold no numbers"
    out.update({"labels": labels, "series": series[:1] if kind == "pie" else series})
    return out, ""


# ------------------------------------------------------------------ road routes
#
# The trip lookup measures stores against a straight line between the two
# cities; OpenRouteService gives the road between them, for the map and for a
# drive time the agent can quote. Only the two city points are sent -- never a
# store. Without a key, or if the service is down, everything falls back to the
# straight line: a trip question never fails because of this.

ORS_URL = "https://api.openrouteservice.org/v2/directions/driving-car/geojson"
ROUTE_TTL = 24 * 3600
ROUTE_MAX_POINTS = 300          # plenty to follow the road on a phone screen
# The road is a nicety; the answer is not. A route that has not come back in
# this long is abandoned for the straight line, and after a failure nobody
# waits on the service again for ROUTE_BACKOFF. On the office server a trip
# question with stores on it never answered: each attempt sat on the route
# call past ElevenLabs' patience, it asked again, and the next attempt sat
# there too. urlopen's own timeout does not cover the DNS lookup, which is
# why the deadline is a thread's, not the socket's.
ROUTE_DEADLINE = 4.0
ROUTE_BACKOFF = 600
_route_lock = threading.Lock()
_route_cache: dict[tuple, dict] = {}
_route_down_until = 0.0


def road_route(from_lat, from_lon, to_lat, to_lon) -> dict | None:
    """{"miles", "minutes", "path": [[lat, lon], ...]} by road, or None --
    within ROUTE_DEADLINE seconds, whatever the network does."""
    global _route_down_until
    key = os.environ.get("ORS_API_KEY", "").strip()
    try:
        a = (float(from_lat), float(from_lon))
        b = (float(to_lat), float(to_lon))
    except (TypeError, ValueError):
        return None
    if not key:
        return None
    cache_key = (round(a[0], 3), round(a[1], 3), round(b[0], 3), round(b[1], 3))
    with _route_lock:
        hit = _route_cache.get(cache_key)
        if hit and hit["at"] > time.time() - ROUTE_TTL:
            return hit["route"]
        if time.time() < _route_down_until:
            return None

    box: list = []
    worker = threading.Thread(target=lambda: box.append(_fetch_route(key, a, b, cache_key)), daemon=True)
    worker.start()
    worker.join(ROUTE_DEADLINE)
    if worker.is_alive() or not box or box[0] is None:
        with _route_lock:
            _route_down_until = time.time() + ROUTE_BACKOFF
        sys.stderr.write("  ROUTE %s: straight line for the next %d min\n"
                         % ("timed out after %.0fs" % ROUTE_DEADLINE if worker.is_alive() else "failed",
                            ROUTE_BACKOFF // 60))
        return None
    return box[0]


def _fetch_route(key: str, a: tuple, b: tuple, cache_key: tuple) -> dict | None:
    """One OpenRouteService request; road_route puts the deadline on it."""
    body = json.dumps({"coordinates": [[a[1], a[0]], [b[1], b[0]]]}).encode("utf-8")
    req = urllib.request.Request(ORS_URL, data=body, method="POST", headers={
        "Authorization": key, "Content-Type": "application/json",
        "Accept": "application/geo+json", "User-Agent": "voiceAI-sales-assistant"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            feature = json.loads(resp.read().decode("utf-8"))["features"][0]
        summary = feature["properties"]["summary"]
        coords = feature["geometry"]["coordinates"]          # [lon, lat]
    except urllib.error.HTTPError as exc:
        # The body says why (quota, bad key) and never echoes the key.
        sys.stderr.write("  ROUTE failed: HTTP %d %s\n" % (exc.code, exc.read()[:200]))
        return None
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        sys.stderr.write("  ROUTE failed: %s\n" % type(exc).__name__)
        return None

    step = max(1, len(coords) // ROUTE_MAX_POINTS)
    path = [[round(lat, 5), round(lon, 5)] for lon, lat in coords[::step]]
    if coords and path[-1] != [round(coords[-1][1], 5), round(coords[-1][0], 5)]:
        path.append([round(coords[-1][1], 5), round(coords[-1][0], 5)])
    route = {"miles": round(summary["distance"] / 1609.344),
             "minutes": round(summary["duration"] / 60), "path": path}
    with _route_lock:
        if len(_route_cache) >= 256:
            _route_cache.pop(next(iter(_route_cache)))
        _route_cache[cache_key] = {"at": time.time(), "route": route}
    return route


def drive_words(route: dict, from_place: str, to_place: str) -> str:
    """The drive as the agent should say it -- worked out here, because
    turning 161 minutes into hours is arithmetic the model gets wrong."""
    minutes = max(5, int(round(route["minutes"] / 5.0)) * 5)
    hours, mins = divmod(minutes, 60)
    took = " ".join(filter(None, [
        "%d hour%s" % (hours, "" if hours == 1 else "s") if hours else "",
        "%d minutes" % mins if mins else ""]))
    return ("From %s to %s is about %d miles by road, about %s of driving without traffic."
            % (from_place, to_place, route["miles"], took))


# ------------------------------------------------------------------ web search
#
# The one lookup that leaves our own data: Tavily, for what the warehouse
# cannot know -- news, when a festival falls this year, what a product is for.
# Never for our prices, stock or accounts. Whatever goes in the query goes to
# an outside company, so store and rep names the conversation has turned up
# are taken out before it leaves. Like the road route, a search that has not
# come back in time is given up on: a slow network costs one sentence, not the
# reply. Without TAVILY_API_KEY the tool is simply not offered.

TAVILY_URL = "https://api.tavily.com/search"
SEARCH_DEADLINE = 5.0
SEARCH_BACKOFF = 120
SEARCH_TTL = 1800
SEARCH_MAX_CHARS = 600          # per result: enough to answer from, little to wade through
_search_lock = threading.Lock()
_search_cache: dict[str, dict] = {}
_search_down_until = 0.0

NO_SEARCH = ("The web search did not run -- it timed out, so nothing was searched. Say you "
             "could not check the web just now (never that you found nothing, or that "
             "there is nothing on), and answer what you can from our own data.")

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the internet for what our own data cannot know: news (tariffs, "
            "shortages, recalls, a price rise on rice or cooking oil), when a holiday "
            "or festival falls this year and what people buy for it, what a product is "
            "or how it is used. Use it only when our lookups cannot answer and the "
            "answer would help the rep sell. NEVER use it for our prices, stock, sales, "
            "customers or reps -- those come only from the other lookups. NEVER put a "
            "store's, customer's or rep's name in the search: search the topic. "
            "Festivals and events fall on different dates in different cities, so for "
            "one store's area put its city and state in the search, and only quote "
            "dates for that place. The "
            "result comes from the web, not from U.S. Trading: say where it came from "
            "('Reuters says...'), keep it to a sentence or two, and if the results do "
            "not answer the question, say you could not find it rather than guess."),
        "parameters": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string", "description": (
                "A short web search, 3 to 8 words, with the year for anything dated "
                "and the city for anything local. For example Hmong New Year 2026 "
                "Madison Wisconsin.")}},
        },
    },
}

# Row fields that name a store or a person. Cities stay: "rice prices
# Chicago" gives nothing away.
PRIVATE_FIELDS = ("customer_name", "customer_key", "owner_rep_name", "owner_rep", "rep_name", "address")
_COMPANY_SUFFIX = re.compile(r"[\s,.]+(llc|inc|corp|co|ltd)\.?$", re.I)


def search_key() -> str:
    return os.environ.get("TAVILY_API_KEY", "").strip()


def private_names(history: list, rep: str = "") -> set[str]:
    """Every store, account key and rep name this conversation's lookups
    returned -- the words that must not leave in a search."""
    names = {rep} if rep else set()
    for message in history:
        if message.get("role") != "tool":
            continue
        try:
            rows = json.loads(message.get("content") or "{}").get("rows") or []
        except (ValueError, AttributeError):
            continue
        for row in rows:
            for field in PRIVATE_FIELDS:
                value = row.get(field) if isinstance(row, dict) else None
                if isinstance(value, str) and len(value.strip()) >= 3:
                    names.add(value.strip())
                    # The rep says "Hmong Legacy Market", the row says "... LLC".
                    names.add(_COMPANY_SUFFIX.sub("", value.strip()))
    return {n for n in names if len(n) >= 3}


def scrub_query(query: str, names: set[str]) -> str:
    for name in sorted(names, key=len, reverse=True):
        query = re.sub(re.escape(name), " ", query, flags=re.I)
    return " ".join(query.split())


def web_search(query: str, history: list, rep: str = "") -> str:
    """One web search as JSON text for the model, within SEARCH_DEADLINE
    seconds whatever the network does."""
    global _search_down_until
    key = search_key()
    if not key:
        return json.dumps({"error": "Web search is not set up. Say you cannot look that up."})
    clean = scrub_query(query, private_names(history, rep))
    if clean.lower() != " ".join(query.split()).lower():
        sys.stderr.write("  SEARCH: took a store or rep name out of the query\n")
    if len(clean) < 3:
        return json.dumps({"error": ("Search for the topic, never a store or a person -- "
                                     "for example 'Hmong New Year 2026 dates'.")})
    cache_key = clean.lower()
    with _search_lock:
        hit = _search_cache.get(cache_key)
        if hit and hit["at"] > time.time() - SEARCH_TTL:
            return json.dumps(hit["result"])
        if time.time() < _search_down_until:
            return json.dumps({"error": NO_SEARCH})

    box: list = []

    def fetch() -> None:
        # A failed DNS lookup comes back at once, and the next one usually
        # works; one more try fits easily inside the deadline.
        started = time.time()
        found = _fetch_search(key, clean)
        if found is None and time.time() - started < SEARCH_DEADLINE / 2:
            found = _fetch_search(key, clean)
        box.append(found)

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    worker.join(SEARCH_DEADLINE)
    if worker.is_alive() or not box or box[0] is None:
        with _search_lock:
            _search_down_until = time.time() + SEARCH_BACKOFF
        sys.stderr.write("  SEARCH %s: no web search for the next %d min\n"
                         % ("timed out after %.0fs" % SEARCH_DEADLINE if worker.is_alive() else "failed",
                            SEARCH_BACKOFF // 60))
        return json.dumps({"error": NO_SEARCH})
    result = box[0]
    with _search_lock:
        if len(_search_cache) >= 256:
            _search_cache.pop(next(iter(_search_cache)))
        _search_cache[cache_key] = {"at": time.time(), "result": result}
    return json.dumps(result)


# A rule in the system prompt to "think about the calendar" did nothing: asked
# what to pitch a Hmong store ten days before Hmong New Year, the model never
# searched unless the rep said "holiday". The same reminder beside the rows,
# at the moment it is choosing what to say, is one it acts on. It still
# decides for itself whether anything is worth a search, and what.
SEASON_CHECK_TOOLS = ("lookup_pitch_list",)
SEASON_CHECK = (
    "Before you answer, think like a seller about timing: is anything happening "
    "in the next six weeks or so that should change this pitch -- a festival or "
    "holiday for the community this store serves (its name and what it buys tell "
    "you which), or news about what it buys? If there plausibly is, call "
    "web_search now with the topic, the store's city and state, and the year, "
    "then use what you find. If nothing plausibly applies, just answer.")


def searched(history: list) -> bool:
    """Whether this conversation has already been to the web."""
    return any(c.get("function", {}).get("name") == "web_search"
               for m in history for c in (m.get("tool_calls") or []))


def with_season_check(result: str) -> str:
    try:
        payload = json.loads(result)
    except ValueError:
        return result
    if not payload.get("rows"):
        return result
    payload["before_you_answer"] = SEASON_CHECK
    return json.dumps(payload)


def _fetch_search(key: str, query: str) -> dict | None:
    """One Tavily request; web_search puts the deadline on it."""
    import datetime
    body = json.dumps({"query": query, "search_depth": "basic", "max_results": 3,
                       "include_answer": True}).encode("utf-8")
    req = urllib.request.Request(TAVILY_URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "Accept": "application/json", "User-Agent": "voiceAI-sales-assistant"})
    try:
        with urllib.request.urlopen(req, timeout=SEARCH_DEADLINE + 3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # The body says why (quota, bad key) and never echoes the key.
        sys.stderr.write("  SEARCH failed: HTTP %d %s\n" % (exc.code, exc.read()[:200]))
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stderr.write("  SEARCH failed: %s\n" % type(exc).__name__)
        return None

    results = []
    for item in (data.get("results") or [])[:3]:
        host = (urllib.parse.urlsplit(item.get("url") or "").hostname or "").removeprefix("www.")
        text = " ".join((item.get("content") or "").split())
        if len(text) > SEARCH_MAX_CHARS:
            text = text[:SEARCH_MAX_CHARS].rsplit(" ", 1)[0] + "..."
        results.append({k: v for k, v in (("title", item.get("title")), ("source", host),
                                          ("published", item.get("published_date")),
                                          ("text", text)) if v})
    return {
        "searched_for": query,
        "searched_on": datetime.date.today().isoformat(),
        "answer": data.get("answer") or "",
        "results": results,
        "note": ("From the web, not U.S. Trading's data. Say where it came from, and never "
                 "use it for our prices, stock or accounts."),
    }


# ------------------------------------------------------------------ visit plans
#
# "What should I pitch them?" used to be answered from one ranked list of ten,
# so the model could only pick from whatever a formula had put at the top --
# and with a festival coming it reached for whatever sounded festive, usually a
# lapsed item. Now the night's work (VISIT_PLAN_TABLE) arrives in sections, one
# per reason to raise something, the store's community holidays come with it,
# and check_store_product lets the model test its own holiday ideas against the
# store's buying. The model decides what to lead with and speaks a summary; the
# whole plan goes to the screen as a card built here from the rows, so nothing
# the model leaves unsaid is lost.

VISIT_PLAN_TABLE = os.environ.get(
    "VISIT_PLAN_TABLE", "ust_databricks.ust_reporting.mart_account_visit_plan_dev").strip()
HOLIDAYS_TABLE = os.environ.get(
    "HOLIDAYS_TABLE", "ust_databricks.ust_reporting.ref_community_holidays").strip()
HOLIDAY_DAYS_AHEAD = 42

PLAN_SECTIONS = (
    ("occasion", "For the occasion"),
    ("season", "Their season: they buy more of these in the next 5 weeks"),
    ("promo_ending", "Promotion ending this week"),
    ("price_drop", "Cheaper than they last paid"),
    ("promo", "On promotion"),
    ("back_in_stock", "Back in stock"),
    ("low_stock", "Our stock is running low: order now"),
    ("peer_gap", "Similar stores buy it; they never have"),
    ("own_brand", "Our own brand instead"),
    ("alternative", "Instead of what we are out of"),
    ("buying_less", "Buying less lately"),
    ("due", "Due now"),
    ("winback", "Win-backs"),
    ("new_trending", "New or trending in what they buy"),
    ("staple", "Staples"),
)
PLAN_ITEMS_TO_MODEL = 5     # per section, in the short version the model chooses from

VISIT_GUIDE = (
    "These sections are a menu of ideas, not a checklist. Choose the two to four angles "
    "that matter most for THIS store right now, and skip the rest: what is urgent (a "
    "holiday days away, a promotion ending, our stock running low), what rests on this "
    "store's own buying rather than other stores', and what is easy to say yes to (a "
    "price drop, something back in stock, our own brand for less). A store that has not "
    "ordered in a long while is itself an angle. Raise each item for its own reason: a "
    "win-back is a win-back, never a holiday item. If one of upcoming_holidays is "
    "celebrated by the community this store serves and the occasion section is empty, "
    "check two or three key ingredients with check_store_product, all in one go, before "
    "recommending any. Then speak a short summary: one sentence per angle, giving the "
    "product family and its brands and the reason -- \"sweet rice, DF and Apple, and "
    "jasmine rice\" -- never an exact item name or size: the rep knows which size suits "
    "the store, and the sizes are on the screen. Offer ideas, not instructions, and leave "
    "the rep room to judge; you might close by asking which of the angles fits this "
    "store best, rather than offering to check stock. Lead with the reason to buy; a "
    "price rise is something to handle, never the reason to pitch. Do not read lists "
    "aloud. End with your angles, families named as they appear above, on their own "
    "lines, like this:\n```plan\n"
    "[{\"angle\": \"Rice for Hmong New Year, 9 days away\", \"items\": [\"Sweet rice\", \"Jasmine rice\"]},\n"
    " {\"angle\": \"Cheaper than last time\", \"items\": [\"Hmong steamer\"]}]\n```")

VISIT_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_visit_plan",
        "description": (
            "Everything worth raising with ONE store on a visit, by reason: what it buys more "
            "of at this time of year, promotions on things it buys, items back in stock, "
            "regular items due, win-backs with their reasons, new or trending items, its "
            "staples -- each with the evidence -- plus the community holidays coming up. Use "
            "it whenever a rep asks what to sell, pitch, bring up or prepare for a store or a "
            "visit. Needs the account key: look the store up by name first if you only "
            "have its name."),
        "parameters": {"type": "object", "required": ["value"], "properties": {
            "value": {"type": "string", "description": "The account key, e.g. HMO032."}}},
    },
}

CHECK_PRODUCT_TOOL = {
    "type": "function",
    "function": {
        "name": "check_store_product",
        "description": (
            "Check one product idea against ONE store's buying: which of our items match, "
            "whether the store buys them, whether it buys more than usual in the weeks before "
            "a date, whether other stores do, and stock and price now. Use it to test an "
            "idea before recommending it -- above all a holiday ingredient. Call it once per "
            "idea; several calls at once is fine."),
        "parameters": {"type": "object", "required": ["customer_key", "product_words"], "properties": {
            "customer_key": {"type": "string", "description": "Account key, e.g. HMO032."},
            "product_words": {"type": "string", "description": (
                "ONE product, as a wholesale catalog label would name it, 1-3 words. Other "
                "names for that same product may follow, separated by | -- e.g. sweet "
                "rice|glutinous rice|sticky rice. A different product is a separate call.")},
            "occasion": {"type": "string", "description": "The holiday it is for, if any."},
            "occasion_date": {"type": "string", "description": "That holiday's date, YYYY-MM-DD."},
            "dish": {"type": "string", "description": "The dish it goes into, if any."},
        }},
    },
}


def dbx_records(sql: str, params: dict) -> list:
    """A query with bound STRING parameters, as name-to-value records."""
    result = common.databricks({
        "warehouse_id": common.DBX_WAREHOUSE, "statement": sql,
        "parameters": [{"name": k, "value": str(v), "type": "STRING"} for k, v in params.items()],
        "wait_timeout": "50s", "on_wait_timeout": "CANCEL",
        "disposition": "INLINE", "format": "JSON_ARRAY",
    })
    status = result.get("status") or {}
    if status.get("state") != "SUCCEEDED":
        sys.stderr.write("  SQL %s: %s\n" % (status.get("state"),
                                              ((status.get("error") or {}).get("message") or "")[:300]))
        raise common.Fail("Databricks returned %s" % status.get("state"))
    return labelled_rows(*common.rows_of(result))


def _account_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def _is_true(value) -> bool:
    return str(value).lower() == "true"


def read_plan_item(r: dict) -> str:
    """One visit-plan row as the facts a rep would want before raising it."""
    section = r.get("section")
    unit = (r.get("price_unit") or "").replace("ip", "innerpack")
    each = ((" an %s" if unit[:1] in "aeiou" else " a %s") % unit) if unit else ""
    facts = []
    times = int(_num(r.get("times_bought")) or 0)
    if times:
        facts.append("bought %d time%s, last on %s" % (times, "" if times == 1 else "s", _day(r["last_bought"])))
        if section == "due" and _num(r.get("median_days_between_purchases")):
            facts.append("usually about every %d days, so due again" % _num(r["median_days_between_purchases"]))
    elif section in ("promo", "new_trending"):
        facts.append("they have never bought it; they buy this category on %d days a year"
                     % int(_num(r.get("category_days")) or 0))
    elif section == "occasion":
        facts.append("they have not bought it from us -- a suggestion")
    lift = _num(r.get("store_lift"))
    if section == "occasion":
        if r.get("holiday_why"):
            facts.append(r["holiday_why"].rstrip("."))
        if lift and lift >= 1.3:
            facts.append("in the 5 weeks before %s they usually buy %.1f times their normal amount"
                         % (r.get("holiday") or "it", lift))
        if (_num(r.get("peer_lift")) or 0) >= 1.15:
            facts.append("stores that celebrate it buy %.1f times their usual amount beforehand"
                         % _num(r["peer_lift"]))
    elif lift and lift >= 1.3:
        facts.append("in the next 5 weeks they usually buy %.1f times their normal amount (2019-2025)" % lift)
    paid, now = _num(r.get("last_paid_unit_price")), _num(r.get("price_now"))
    if paid and now and abs(now - paid) >= 0.01:
        facts.append("last paid %s%s, %s now (%d%% %s)" % (
            _price(paid), each, _price(now), round(abs(now / paid - 1) * 100), "more" if now > paid else "less"))
    elif now:
        facts.append("%s%s" % (_price(now), each))
    if section == "back_in_stock" and r.get("oos_from"):
        facts.append("U.S. Trading was out of it from %s to %s; back in stock now"
                     % (_day(r["oos_from"]), _day(r["oos_to"])))
    if int(_num(r.get("oos_days_when_due")) or 0):
        facts.append("we were out of stock for %d days around when they were due (%s to %s)" % (
            _num(r["oos_days_when_due"]), _day(r["first_oos_when_due"]), _day(r["last_oos_when_due"])))
    if int(_num(r.get("backorder_requests")) or 0):
        facts.append("they asked for it while we were out, last on %s" % _day(r["last_backorder_date"]))
    if _is_true(r.get("is_on_promotion")):
        buy, free = _num(r.get("promo_buy_qty")), _num(r.get("promo_free_qty"))
        promo = "on promotion"
        if buy and free:
            promo += ": buy %d get %d free" % (buy, free)
        elif _num(r.get("promo_price")):
            promo += " at %s" % _price(r["promo_price"])
        if r.get("promo_end_date"):
            promo += " until %s" % _day(r["promo_end_date"])
        facts.append(promo)
    if _is_true(r.get("is_trending")):
        facts.append("trending on social media this week")
    elif _is_true(r.get("is_new_item")) and section == "new_trending":
        facts.append("a new item")
    if section == "promo_ending" and r.get("promo_days_left") not in (None, ""):
        facts.append("the promotion ends in %d days" % int(_num(r["promo_days_left"])))
    if section == "low_stock" and _num(r.get("stock_vs_usual")) is not None:
        facts.append("our stock is down to %d%% of its usual level -- worth ordering now"
                     % round(_num(r["stock_vs_usual"]) * 100))
    if section == "buying_less" and _num(r.get("recent_vs_prior")) is not None:
        facts.append("in the last 6 months they bought %d%% of what they bought in the 6 before"
                     % round(_num(r["recent_vs_prior"]) * 100))
    if section in ("alternative", "own_brand") and r.get("alt_for"):
        # Case prices only, each with its case size: a case of 6 at $36 against
        # a case of 24 at $82 is no saving, and what is in each unit is not in
        # the data -- so no "cheaper" or percentage here.
        other = _title_case(r["alt_for"])
        theirs = _num(r.get("alt_for_price"))
        ours_n, theirs_n = _num(r.get("case_qty")), _num(r.get("alt_for_case_qty"))

        def case(price, n):
            return "%s for a case of %d" % (_price(price), n) if price and n else (_price(price) if price else "")
        compare = (" (ours %s; theirs %s -- compare what is in each before quoting a saving)"
                   % (case(now, ours_n), case(theirs, theirs_n))) if now and theirs else ""
        facts.append(("in place of %s, which they buy and we are out of%s" if section == "alternative"
                      else "our own brand, in place of %s, which they buy%s") % (other, compare))
    if _is_true(r.get("pack_change_suspect")):
        facts.append("its case price changed by a third or more for every store in recent months -- "
                     "likely a new pack size, so check before comparing prices")
    if section == "peer_gap" and _num(r.get("peer_share")):
        facts.append("%d%% of stores in the same community buy it; this store never has"
                     % round(_num(r["peer_share"]) * 100))
    facts.append("in stock" if _is_true(r.get("is_in_stock")) else "OUT OF STOCK now -- do not lead with it")
    return "%s: %s." % (_title_case(r.get("item_label")), "; ".join(facts))


def lookup_visit_plan(value: str, rep: str = "") -> str:
    key = _account_key(value)
    if not re.fullmatch(r"[A-Z]{3}\d{3}", key):
        return json.dumps({"error": "That is not an account key. Find the store with "
                                    "find_customer_by_name first, then pass its key."})
    rep_clause = " AND p.owner_rep_name = :rep_name" if rep else ""
    params = {"key": key, **({"rep_name": rep} if rep else {})}
    box: dict = {}

    def plan():
        box["rows"] = dbx_records(
            "SELECT p.*, c.customer_name, c.city FROM %s p "
            "LEFT JOIN ust_databricks.ust_dims.dim_customers c USING (customer_key) "
            "WHERE p.customer_key = :key%s ORDER BY p.section, p.rank_in_section"
            % (VISIT_PLAN_TABLE, rep_clause), params)

    def account():
        # How the store is doing as a whole: an account well past its usual
        # gap is an angle for the visit in itself.
        box["account"] = dbx_records(
            "SELECT usual_gap_days, days_since_last_invoice_any_rep AS days_since_last_order, "
            "rhythm_status FROM ust_databricks.ust_reporting.mart_rep_account_status "
            "WHERE customer_key = :key LIMIT 1", {"key": key})

    try:
        workers = [threading.Thread(target=f) for f in (plan, account)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        rows = box["rows"]
    except (common.Fail, KeyError) as exc:
        return json.dumps({"error": str(exc) or "The visit plan could not be read."})
    if not rows:
        return json.dumps({"error": "No visit plan for %s%s. Check the key, or whether it is "
                                    "one of this rep's accounts." % (key, " for " + rep if rep else "")})
    full, slim = {}, {}
    for r in rows:
        extra = ({"holiday": r["holiday"], "holiday_date": r.get("holiday_date"), "dish": r.get("dish"),
                  "days_until": r.get("days_until")} if r.get("holiday") else {})
        case_n = _num(r.get("case_qty"))
        pack = " · ".join(x for x in (
            ("%s of %d" % (r.get("price_unit") or "case", case_n)) if case_n else "",
            _price(r["price_now"]) if _num(r.get("price_now")) else "") if x)
        full.setdefault(r["section"], []).append({
            "item": _title_case(r.get("item_label")), "detail": read_plan_item(r), "why": plan_reason(r),
            "pack": pack, "can_lead": _is_true(r.get("can_lead")), **extra})
        slim.setdefault(r["section"], []).append({
            "item": _title_case(r.get("item_label")), "why": plan_reason(r), **extra})
    first = rows[0]
    store = {"customer_key": key, "name": _title_case(first.get("customer_name")),
             "city": _title_case(first.get("city")), "plan_as_of": first.get("as_of_date")}
    order = [name for name, _ in PLAN_SECTIONS]
    # The model gets each item's name and one reason; the screen card and
    # lookup_plan_item get the whole of it. Handed all 40-odd rows in full,
    # the model answered the first question well and then, a turn later, could
    # not find the cooking pot in them and invented a price for the steamer.
    holidays = sorted({(e["holiday_date"], e["holiday"], e.get("days_until"))
                       for e in full.get("occasion", []) if e.get("holiday_date")})
    with _plan_lock:
        if len(_plan_cache) >= 256:
            _plan_cache.pop(next(iter(_plan_cache)))
        _plan_cache[(key, rep)] = {"at": time.time(), "store": store,
                                   "sections": {n: full[n] for n in order if n in full}}
    acct = (box.get("account") or [{}])[0]
    titles = dict(PLAN_SECTIONS)
    return json.dumps({
        "store": store,
        "account": ("usually orders about every %s days; last order %s days ago (%s)"
                    % (acct.get("usual_gap_days"), acct.get("days_since_last_order"), acct.get("rhythm_status"))
                    if acct.get("days_since_last_order") else "no ordering rhythm on record"),
        "possible_reasons_account_is_slowing": slowing_reasons(rows) or ["nothing in the data points to one"],
        "store_holidays": [{"holiday": h, "date": d, "days_until": n} for d, h, n in holidays],
        # By product family: "Sweet rice (Dragonfly, Apple): <why>" -- what the
        # rep pitches -- with how many sizes sit behind it.
        "ideas": {titles[n]: [
            {"family": g["family"], "brands": g["brands"], "why": g["members"][0]["why"],
             "items": len(g["members"]),
             **({"holiday": g["members"][0]["holiday"], "days_until": g["members"][0].get("days_until")}
                if g["members"][0].get("holiday") else {})}
            for g in by_family(slim[n])][:PLAN_ITEMS_TO_MODEL if n != "occasion" else None]
            for n in order if n in slim},
        "details": "Each item's full facts are on the rep's screen; for one item's facts "
                   "in a later answer, use lookup_plan_item.",
        "before_you_answer": VISIT_GUIDE,
    })


_plan_lock = threading.Lock()
_plan_cache: dict[tuple, dict] = {}


# ------------------------------------------------------------------ product families
#
# Reps pitch "sweet rice -- DF and Apple", not "DF Thai Sweet Rice (10X5LBS)":
# they know which size suits which store, and a list of exact items reads as
# instructions rather than ideas. So items are grouped by what the product is,
# with the brand and the codes in front of the name read the way the sales team
# reads them, and the sizes kept underneath.

# Codes at the start of an item name (from the sales team), longest first.
LABEL_PREFIXES = (
    ("FZ DF", "Dragonfly", "frozen"), ("FZ (DF)", "Dragonfly", "frozen"), ("FZ(DF)", "Dragonfly", "frozen"),
    ("DFZ", "", "discontinued frozen"), ("FZ", "", "frozen"), ("DF", "Dragonfly", ""),
    ("DRAGONFLY", "Dragonfly", ""), ("HBB", "Honey Bee", ""), ("HB", "Honey Bee", ""), ("CG", "Chung Guang", ""),
)
KNOWN_BRANDS = ("APPLE", "PINK LOTUS", "AROY D", "AROY-D", "CHAOKOH", "MAMA", "KIKKOMAN", "LKK", "JHC", "TAS",
                "TASCO", "THREE CRAB", "RED BOAT", "PANTAI", "SQUID", "ERAWAN", "MAEKRUA", "MOGU MOGU", "COZY",
                "FOCO", "HONEYBEE", "KHS", "NESTLE", "MAGGI", "TEANS", "TEAN'S", "VITAMILK", "SAMYANG", "G7",
                "KIWI", "ALUM", "BLACK & WHITE", "NORA", "BB", "999", "LAURIAT", "MEIJI", "KYJ", "SUNAREE",
                "JOYLUCK", "NS", "LION & GLOBE", "ANGEL", "OCEAN", "WAN JA SHAN", "LEE KUM KEE")
_PACK_WORDS = {"can", "bottle", "btl", "pkg", "pkt", "pkts", "pack", "pk", "box", "bag", "jar", "carton", "bulk",
               "large", "small", "medium", "l", "m", "s", "xl", "xs", "reg", "regular", "n/c", "nc",
               "oz", "lb", "lbs", "g", "kg", "ml", "cm", "pcs", "pc", "ct"}
_SIZE_WORD = re.compile(r"^\d+(\.\d+)?([x/]\d+)*(oz|lb|lbs|g|kg|ml|l|cm|pc|pcs|pk|ct|n)?$", re.I)
_VARIETY_WORDS = {"thai", "usa", "brand", "pure", "premium", "original", "new", "hot", "spicy", "mild", "lite",
                  "light", "dark", "super", "extra", "fresh", "family", "jumbo", "baby", "whole", "sliced",
                  "slice", "strip", "tip"}
_PRODUCT_NOUNS = {"rice", "noodle", "noodles", "sauce", "milk", "cream", "drink", "juice", "tea", "coffee", "soda",
                  "sausage", "steamer", "pot", "oil", "mix", "broth", "paste", "flour", "sugar", "chili", "chips",
                  "cracker", "crackers", "laver", "vermicelli", "bean", "beans", "shoot", "shoots", "jam", "powder",
                  "seasoning", "vinegar", "candy", "cake", "snack", "water", "wine", "dessert", "jelly", "salt",
                  "cookie", "wafer", "soup", "belly", "skin", "pho", "paper", "knife", "eggs", "egg", "syrup"}


def product_family(label: str) -> tuple[str, str, list]:
    """("Sweet rice", "Dragonfly", []) for "DF THAI SWEET RICE (10X5LBS)":
    what the product is, its brand, and flags such as frozen."""
    text = " ".join(str(label or "").upper().split())
    brand, flags = "", []
    for code, name, flag in LABEL_PREFIXES:
        if text == code or text.startswith(code + " "):
            brand, text = name, text[len(code):].strip()
            flags += [flag] if flag else []
            break
    text = re.sub(r"\([^)]*\)?", " ", text)          # (10X5LBS) (L) (USA) -- and an unclosed one
    words = [w for w in re.findall(r"[A-Z0-9/'&.-]+", text)
             if w.lower() not in _PACK_WORDS and not _SIZE_WORD.match(w) and not re.fullmatch(r"[\d.]+", w)]
    rest = " ".join(words)
    if not brand:
        for b in sorted(KNOWN_BRANDS, key=len, reverse=True):
            if rest == b or rest.startswith(b + " "):
                brand, rest = b.title(), rest[len(b):].strip()
                break
    core = [w for w in rest.split() if w.lower() not in _VARIETY_WORDS]
    nouns = [i for i, w in enumerate(core) if w.lower() in _PRODUCT_NOUNS]
    # The product noun and up to two words before it ("coconut milk drink",
    # "tom yum noodle"); a flavour after it ("noodle beef flavor") goes.
    core = core[max(0, nouns[-1] - 2):nouns[-1] + 1] if nouns else core[-2:]
    return (" ".join(core).capitalize() or _title_case(label)), brand, flags


def display_name(label: str) -> str:
    """An item's name with its codes read out: "Dragonfly Sugarcane Herbal
    Drink (frozen)" for "FZ DF SUGARCANE HERBAL DRINK"."""
    text = " ".join(str(label or "").split())
    for code, name, flag in LABEL_PREFIXES:
        if text.upper() == code or text.upper().startswith(code + " "):
            rest = _title_case(text[len(code):].strip().upper())
            return ("%s %s" % (name, rest) if name else rest) + (" (%s)" % flag if flag else "")
    return _title_case(text)


def by_family(entries: list) -> list:
    """Entries of one section grouped by product family, in the section's own
    order (the first member is the strongest)."""
    groups: dict[str, dict] = {}
    for e in entries:
        fam, brand, _ = product_family(e["item"])
        g = groups.setdefault(fam.lower(), {"family": fam, "brands": [], "members": []})
        if brand and brand not in g["brands"]:
            g["brands"].append(brand)
        g["members"].append(e)
    return list(groups.values())


def plan_reason(r: dict) -> str:
    """One short reason for a visit-plan item -- what the model chooses from."""
    section = r.get("section")
    bits = []
    if section == "occasion":
        bits.append("for %s (%s, in %s days)" % (r.get("holiday"), _day(r["holiday_date"]),
                                                 int(_num(r.get("days_until")) or 0)))
        if (_num(r.get("store_lift")) or 0) >= 1.3:
            bits.append("buys %.1fx its usual amount before it" % _num(r["store_lift"]))
        elif (_num(r.get("peer_lift")) or 0) >= 1.15:
            bits.append("stores that celebrate it buy %.1fx usual beforehand" % _num(r["peer_lift"]))
        bits.append("they buy it" if _num(r.get("times_bought")) else "never bought by them -- a suggestion")
    elif section == "season" and _num(r.get("store_lift")):
        bits.append("buys %.1fx its usual amount in the next 5 weeks" % _num(r["store_lift"]))
    elif section == "promo":
        buy, free = _num(r.get("promo_buy_qty")), _num(r.get("promo_free_qty"))
        bits.append("on promotion" + ((": buy %d get %d free" % (buy, free)) if buy and free else ""))
        bits.append("they buy it" if _num(r.get("times_bought")) else "new to them; they buy the category")
    elif section == "back_in_stock" and r.get("oos_from"):
        bits.append("we were out %s to %s; back in stock" % (_day(r["oos_from"]), _day(r["oos_to"])))
    elif section in ("due", "winback"):
        # Why it has not been reordered, where the data says: asked "why did
        # they stop buying the cooking pot?", the model -- seeing only "due
        # again" -- said it had no idea, with the stockout in the full row.
        why = []
        if int(_num(r.get("oos_days_when_due")) or 0):
            why.append("we were out of stock for %d days when they were due" % _num(r["oos_days_when_due"]))
        if int(_num(r.get("backorder_requests")) or 0):
            why.append("they asked for it while we were out")
        if section == "due":
            bits.append("regular item, last bought %s, due again" % _day(r["last_bought"]) if r.get("last_bought")
                        else "regular item, due again")
        else:
            bits.append("lapsed since %s" % (_day(r["last_bought"]) if r.get("last_bought") else "?"))
        bits += why
    elif section == "promo_ending":
        buy, free = _num(r.get("promo_buy_qty")), _num(r.get("promo_free_qty"))
        bits.append("promotion%s ends in %d days" % ((" (buy %d get %d free)" % (buy, free)) if buy and free else "",
                                                    int(_num(r.get("promo_days_left")) or 0)))
    elif section == "price_drop":
        bits.append("they buy it")
    elif section == "low_stock":
        bits.append("they buy it; our stock is at %d%% of usual" % round((_num(r.get("stock_vs_usual")) or 0) * 100))
    elif section == "buying_less":
        bits.append("last 6 months at %d%% of the 6 before" % round((_num(r.get("recent_vs_prior")) or 0) * 100))
    elif section == "alternative":
        bits.append("in place of %s, which we are out of" % _title_case(r.get("alt_for")))
    elif section == "own_brand":
        bits.append("our own brand in place of %s; case sizes differ, so no saving to quote"
                    % _title_case(r.get("alt_for"))
                    if _num(r.get("case_qty")) != _num(r.get("alt_for_case_qty"))
                    else "our own brand in place of %s" % _title_case(r.get("alt_for")))
    elif section == "peer_gap":
        bits.append("%d%% of similar stores buy it; never bought here" % round((_num(r.get("peer_share")) or 0) * 100))
    elif section == "new_trending":
        bits.append("trending on social" if _is_true(r.get("is_trending")) else "new item")
        bits.append("never bought; they buy the category")
    elif section == "staple":
        bits.append("bought on %d days in the last year" % int(_num(r.get("days_bought_365")) or 0))
    paid, now = _num(r.get("last_paid_unit_price")), _num(r.get("price_now"))
    if _is_true(r.get("pack_change_suspect")):
        bits.append("case price changed for every store lately -- likely a new pack size")
    elif paid and now and abs(now / paid - 1) >= 0.05:
        bits.append("price %s %d%% since they last paid" % ("up" if now > paid else "down",
                                                          round(abs(now / paid - 1) * 100)))
    if not _is_true(r.get("is_in_stock")):
        bits.append("OUT OF STOCK")
    return "; ".join(bits)


def slowing_reasons(rows: list) -> list:
    """Likely reasons an account has slowed, from its own plan: items we were
    out of when they came to reorder (or lately), big price rises on what they
    buy, items they buy far less of. Asked "why haven't they ordered in so
    long?", the model -- with none of this joined up -- said there was no
    specific reason, with the stockouts in the same plan."""
    seen, when_due, lately, rises, less = set(), [], [], [], []
    # Out when they came to reorder first, longest first: that is the likelier cause.
    for r in sorted(rows, key=lambda r: -(_num(r.get("oos_days_when_due")) or 0)):
        name = _title_case(r.get("item_label"))
        if int(_num(r.get("oos_days_when_due")) or 0) and name not in seen:
            # Weeks given too: asked to say it, the model turned 56 days into
            # "about 5 or 6 weeks".
            days = int(_num(r["oos_days_when_due"]))
            when_due.append("%s (out %d days, about %d weeks, when they were due)"
                            % (name, days, max(1, round(days / 7))))
            seen.add(name)
    for r in rows:
        name = _title_case(r.get("item_label"))
        if r.get("section") == "back_in_stock" and r.get("oos_from") and name not in seen:
            lately.append("%s (out %s to %s)" % (name, _day(r["oos_from"]), _day(r["oos_to"])))
            seen.add(name)
    stockouts = when_due + lately
    for r in rows:
        name = _title_case(r.get("item_label"))
        paid, now = _num(r.get("last_paid_unit_price")), _num(r.get("price_now"))
        if (paid and now and now >= paid * 1.15 and not _is_true(r.get("pack_change_suspect"))
                and r.get("section") in ("due", "staple", "winback") and name not in {x[0] for x in rises}):
            rises.append((name, round((now / paid - 1) * 100)))
        if r.get("section") == "buying_less" and name not in less:
            less.append(name)
    out = []
    if stockouts:
        out.append("we were out of stock on things they buy: " + "; ".join(stockouts[:4]))
    if rises:
        rises.sort(key=lambda x: -x[1])
        out.append("prices are up 15%% or more since they last paid on %d of their items, e.g. %s"
                   % (len(rises), "; ".join("%s +%d%%" % x for x in rises[:3])))
    if less:
        out.append("they are buying much less of: " + "; ".join(less[:4]))
    return out


PLAN_ITEM_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_plan_item",
        "description": (
            "The full facts for one item in a store's visit plan -- what they paid, the price "
            "now, stock, how often they buy it, any stockout or promotion. Use it when the rep "
            "asks about one item after a visit plan: never answer from memory."),
        "parameters": {"type": "object", "required": ["customer_key", "item"], "properties": {
            "customer_key": {"type": "string", "description": "Account key, e.g. HMO032."},
            "item": {"type": "string", "description": "A few words of the item's name, e.g. cooking pot."},
        }},
    },
}


def lookup_plan_item(args: dict, rep: str = "") -> str:
    key = _account_key(args.get("customer_key"))
    want = set(re.findall(r"[a-z0-9]+", (args.get("item") or "").lower()))
    if not want:
        return json.dumps({"error": "Say which item, e.g. cooking pot."})
    with _plan_lock:
        cached = _plan_cache.get((key, rep))
    if not cached:
        json.loads(lookup_visit_plan(key, rep))       # fills the cache, or there is no plan
        with _plan_lock:
            cached = _plan_cache.get((key, rep))
    if not cached:
        return json.dumps({"error": "No visit plan for %s." % key})
    found = []
    for section, entries in cached["sections"].items():
        for e in entries:
            words = set(re.findall(r"[a-z0-9]+", e["item"].lower()))
            if want <= words or len(want & words) >= max(1, len(want) - 1):
                found.append({"section": dict(PLAN_SECTIONS).get(section, section), "facts": e["detail"]})
    return json.dumps({"item": args.get("item"), "found": found[:4]} if found else
                      {"error": "No item like %r in that store's visit plan. Try other words, "
                                "or search_items." % args.get("item")})


def _match_clause(words: str) -> tuple[str, dict]:
    """'sweet rice|glutinous rice' as a bound LIKE clause on item_label."""
    params, ors = {}, []
    for i, variant in enumerate((words or "").split("|")[:4]):
        ands = []
        for j, word in enumerate([w for w in re.findall(r"[a-z0-9]+", variant.lower()) if len(w) > 1][:4]):
            params["w%d_%d" % (i, j)] = "%" + word + "%"
            ands.append("lower(item_label) LIKE :w%d_%d" % (i, j))
        if ands:
            ors.append("(" + " AND ".join(ands) + ")")
    return ("(" + " OR ".join(ors) + ")") if ors else "FALSE", params


def check_store_product(args: dict, rep: str = "") -> str:
    key = _account_key(args.get("customer_key"))
    words = args.get("product_words") or ""
    match, params = _match_clause(words)
    if match == "FALSE":
        return json.dumps({"error": "Give the product in a few words, e.g. sweet rice|sticky rice."})
    date = re.match(r"(\d{4})-(\d{2})-(\d{2})", args.get("occasion_date") or "")
    params.update({"key": key, "mo": date.group(2) if date else "1", "dy": date.group(3) if date else "1"})
    before = ("datediff(make_date(year(p.posting_date), CAST(:mo AS INT), CAST(:dy AS INT)), "
              "p.posting_date) BETWEEN 0 AND 34")
    try:
        rows = dbx_records("""
            WITH items AS (
                SELECT item_no, item_label, unit_price, is_in_stock, shippable_qty
                FROM ust_databricks.ust_dims.dim_items WHERE is_sellable AND NOT is_blocked AND %(match)s),
            store AS (
                SELECT p.item_no, count(DISTINCT p.posting_date) AS times_bought, max(p.posting_date) AS last_bought,
                       sum(CASE WHEN year(p.posting_date) BETWEEN 2019 AND 2025 AND %(before)s
                                THEN p.sold_quantity ELSE 0 END) / 7 AS before_qty,
                       count(DISTINCT CASE WHEN year(p.posting_date) BETWEEN 2019 AND 2025 AND %(before)s
                                           THEN p.posting_date END) AS before_buys,
                       sum(CASE WHEN year(p.posting_date) BETWEEN 2019 AND 2025 THEN p.sold_quantity ELSE 0 END)
                           / (7 * 365 / 35.0) AS usual_qty
                FROM ust_databricks.ust_intermediate.int_customer_item_purchases p JOIN items i USING (item_no)
                WHERE p.customer_key = :key AND p.sold_quantity > 0 GROUP BY p.item_no),
            everyone AS (
                SELECT p.item_no, sum(CASE WHEN %(before)s THEN p.sold_quantity ELSE 0 END) / 3 AS before_qty,
                       sum(p.sold_quantity) / 39.0 AS usual_qty, count(DISTINCT p.customer_key) AS stores
                FROM ust_databricks.ust_intermediate.int_customer_item_purchases p JOIN items i USING (item_no)
                WHERE p.posting_date >= '2023-01-01' AND p.posting_date < '2026-01-01' AND p.sold_quantity > 0
                GROUP BY p.item_no)
            SELECT i.item_label, i.unit_price, i.is_in_stock, i.shippable_qty,
                   coalesce(s.times_bought, 0) AS times_bought, s.last_bought,
                   CASE WHEN s.before_buys >= 3 THEN round(s.before_qty / nullif(s.usual_qty, 0), 2) END AS store_lift,
                   CASE WHEN e.stores >= 3 THEN round(e.before_qty / nullif(e.usual_qty, 0), 2) END AS all_stores_lift,
                   (SELECT first(owner_rep_name) FROM ust_databricks.ust_dims.dim_customers
                     WHERE customer_key = :key) AS owner_rep_name
            FROM items i LEFT JOIN store s USING (item_no) LEFT JOIN everyone e USING (item_no)
            ORDER BY times_bought DESC, i.is_in_stock DESC LIMIT 3""" % {"match": match, "before": before},
            params)
    except common.Fail as exc:
        return json.dumps({"error": str(exc)})
    if rows and rep and (rows[0].get("owner_rep_name") or "") != rep:
        return json.dumps({"error": "That is not one of %s's accounts." % rep})
    matches = []
    for r in rows:
        facts = []
        times = int(_num(r.get("times_bought")) or 0)
        facts.append(("this store bought it %d times, last on %s" % (times, _day(r["last_bought"])))
                     if times else "this store has never bought it from us")
        if date:
            lift, others = _num(r.get("store_lift")), _num(r.get("all_stores_lift"))
            if lift is not None:
                facts.append(("in the 5 weeks before this date it buys %.1f times its usual amount" % lift)
                             if lift >= 1.3 else "it buys no more than usual before this date")
            if others is not None and others >= 1.15:
                facts.append("our stores overall buy %.1f times their usual amount before it" % others)
        stock = _is_true(r.get("is_in_stock"))
        facts.append(("in stock, %s" % _price(r["unit_price"])) if stock else "OUT OF STOCK now -- do not lead with it")
        matches.append({"item": _title_case(r.get("item_label")), "can_lead": stock,
                        "detail": "%s: %s." % (_title_case(r.get("item_label")), "; ".join(facts))})
    return json.dumps({
        "checked": words, "occasion": args.get("occasion") or "", "occasion_date": args.get("occasion_date") or "",
        "dish": args.get("dish") or "", "matches": matches,
        "note": ("Nothing in our catalog matched." if not matches else
                 "Recommend a match for the occasion only where the store buys it, or buys more "
                 "before the date; say what it rests on.")})


# ------------------------------------------------------------------ fact check
#
# A prompt cannot stop the model inventing a figure. Asked "tell me more about
# the steamer" a turn after the visit plan, it said "$12.50, 142 units, buy 10
# get 2 free" -- none of it anywhere in the data. So before a reply is spoken,
# every price, percentage, multiple, quantity and date in it must appear in a
# lookup result from this call (or in what the rep said). If one does not, the
# model gets one chance to correct it; whatever is still unsupported after
# that is taken out, sentence by sentence.

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_FACT_PATTERNS = (
    ("money", re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")),
    ("percent", re.compile(r"(\d+(?:\.\d+)?)\s?%")),
    ("times", re.compile(r"(\d+(?:\.\d+)?)\s?(?:x|×|times)\b", re.I)),
    ("count", re.compile(r"(\d[\d,]*)\s+(?:units|cases|boxes|bags|packs|orders|stores|lbs|pounds|days|weeks)\b", re.I)),
    # Never from the middle of a figure: "$1,080,000" is one number, not "080,000".
    ("number", re.compile(r"(?<![\d.,$])(\d{3,}(?:,\d{3})*(?:\.\d+)?)(?![\d%,])")),
)
_DATE_IN_TEXT = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)
_ISO_DATE = re.compile(r"\b20\d\d-(\d\d)-(\d\d)\b")


def _evidence_text(history: list, rep: str) -> str:
    """Everything this call's answer may draw figures from: every lookup result
    (earlier turns' included -- with_evidence put them back), the full visit
    plans behind the short versions the model saw, and the rep's own words."""
    parts = []
    for m in history:
        if m.get("role") == "tool" or m.get("role") == "user":
            parts.append(str(m.get("content") or ""))
            if m.get("role") == "tool" and '"store"' in str(m.get("content") or ""):
                try:
                    key = json.loads(m["content"])["store"]["customer_key"]
                except (ValueError, KeyError, TypeError):
                    continue
                with _plan_lock:
                    cached = _plan_cache.get((key, rep))
                if cached:
                    parts += [e["detail"] for entries in cached["sections"].values() for e in entries]
    return "\n".join(parts)


def unsupported_facts(answer: str, evidence: str) -> list:
    text = _VISUAL_FENCE.sub("", answer or "")
    numbers = set()
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", evidence):
        try:
            numbers.add(round(float(raw.replace(",", "")), 2))
        except ValueError:
            pass
    dates = {(int(a), int(b)) for a, b in _ISO_DATE.findall(evidence)}
    dates |= {(_MONTHS[m[:3].lower()], int(d)) for m, d in _DATE_IN_TEXT.findall(evidence)}
    tolerance = {"money": 0.01, "percent": 0.5, "times": 0.05, "count": 0, "number": 0.01}
    missing = []
    for kind, pattern in _FACT_PATTERNS:
        for found in pattern.finditer(text):
            try:
                value = float(found.group(1).replace(",", ""))
            except ValueError:
                continue
            # Whole dollars are rounded on purpose ($936,051 for 936051.40).
            allow = 0.5 if kind in ("money", "number") and "." not in found.group(1) else tolerance[kind]
            if not any(abs(value - n) <= allow for n in numbers):
                missing.append(found.group(0).strip())
    for m, d in _DATE_IN_TEXT.findall(text):
        if (_MONTHS[m[:3].lower()], int(d)) not in dates:
            missing.append("%s %s" % (m, d))
    return sorted(set(missing))


def fact_checked(answer: str, history: list, model: str, max_tokens: int, rep: str) -> str:
    evidence = _evidence_text(history, rep)
    missing = unsupported_facts(answer, evidence)
    if not missing:
        return answer
    sys.stderr.write("  FACT CHECK: not in the data: %s -- asking for a correction\n" % ", ".join(missing))
    correction = [
        {"role": "assistant", "content": answer},
        {"role": "user", "content": (
            "Before that is spoken: these figures are in no lookup result in this conversation: "
            "%s. Write the answer again using only figures that came back from a lookup. If "
            "you need one you do not have, leave it out or offer to look it up. Keep any lead "
            "block. Reply with the corrected answer only." % ", ".join(missing))},
    ]
    try:
        fixed = (((call_upstream_llm(history + correction, model, max_tokens, []).get("choices") or [{}])[0])
                 .get("message") or {}).get("content") or ""
    except UpstreamError:
        fixed = ""
    fixed = fixed.strip() or answer
    still = unsupported_facts(fixed, evidence)
    if not still:
        return fixed
    # Still there: take out the sentences that carry them.
    sys.stderr.write("  FACT CHECK: still unsupported: %s -- removing those sentences\n" % ", ".join(still))
    blocks = "".join(m.group(0) for m in _VISUAL_FENCE.finditer(fixed))
    prose = _VISUAL_FENCE.sub("", fixed)
    kept = [s for s in re.split(r"(?<=[.!?])\s+", prose) if not any(f in s for f in still)]
    text = " ".join(kept).strip()
    if not re.search(r"[A-Za-z]{3}", text):
        text = "I don't have that figure in front of me. Want me to look it up?"
    return (text + ("\n" + blocks if blocks else "")).strip()


_LEAD_BLOCK = re.compile(r"```[ \t]*lead[ \t]*\n?(.*?)```", re.S | re.I)


_TOOL_MARKUP = re.compile(r"<\|?tool_call\|?>.*?(?:<\|?tool_call\|?>|$)|\bcall:\w+\{.*?\}", re.S)


def without_tool_markup(answer: str) -> str:
    """The reply with any tool call the model wrote out as text taken out.

    Out of rounds and offered no tools, the model wrote its next lookup as
    "<|tool_call>call:lookup_visit_plan{...}<tool_call|>", and the voice read
    it to the rep. Whatever is left is the answer; if nothing is, say so."""
    if not _TOOL_MARKUP.search(answer or ""):
        return answer
    sys.stderr.write("  LLM proxy: tool-call text in the reply, removed\n")
    text = " ".join(_TOOL_MARKUP.sub(" ", answer).split())
    # "Let me check." on its own is not an answer -- the rep heard it already.
    return text if len(text.split()) >= 6 else (
        "Sorry, I couldn't pull that up just now. Could you ask me once more?")


def _plan_shown_before(messages: list, card: dict) -> bool:
    """Whether an earlier turn of this call already looked up this store's plan."""
    for m in messages:
        if m.get("role") == "tool" and '"store"' in str(m.get("content") or ""):
            try:
                store = json.loads(m["content"]).get("store") or {}
            except (ValueError, AttributeError):
                continue
            if card.get("title", "").startswith("Visit plan: %s," % store.get("name")):
                return True
    return False


def drop_dangling_intro(answer: str) -> str:
    """"The full plan is on your screen, but I'd lead with these:" introduced
    the lead block, which is not spoken -- so the voice ended on a list that
    never came. The clause after the last comma goes, or the whole sentence if
    it has no comma."""
    text = (answer or "").rstrip()
    if not text.endswith(":"):
        return answer
    body = text[:-1]
    start = max(body.rfind(". "), body.rfind("! "), body.rfind("? "))
    cut = max(body.rfind(","), body.rfind(";"))
    if cut > start:
        return body[:cut].rstrip() + "."
    return body[:start + 1].strip() if start >= 0 else answer


_PLAN_BLOCK = re.compile(r"```[ \t]*plan[ \t]*\n?(.*?)```", re.S | re.I)


def take_angles(answer: str) -> list:
    """The angles the model chose for this visit, from its ```plan block:
    [{"angle": "...", "items": ["...", ...]}, ...]. The block itself goes with
    the other fenced blocks in take_visuals."""
    found = _PLAN_BLOCK.search(answer or "")
    if not found:
        return []
    try:
        angles = json.loads(found.group(1).strip())
    except ValueError:
        return []
    out = []
    for a in angles if isinstance(angles, list) else []:
        if isinstance(a, dict) and a.get("angle") and isinstance(a.get("items"), list):
            out.append({"angle": " ".join(str(a["angle"]).split())[:90],
                        "items": [str(i) for i in a["items"] if i][:5]})
    return out[:5]


def take_leads(answer: str) -> list:
    """The item names in the answer's ```lead block (the block itself goes with the
    other fenced blocks in take_visuals)."""
    found = _LEAD_BLOCK.search(answer or "")
    if not found:
        return []
    try:
        names = json.loads(found.group(1).strip())
    except ValueError:
        names = re.findall(r'"([^"]+)"', found.group(1))
    return [str(n) for n in names if n][:6] if isinstance(names, list) else []


def _is_lead(item: str, leads: list) -> bool:
    """Every word of a lead pick in the item's name: "Alum Hmong Steamer" marks
    "Alum Hmong Steamer (28Cm)", and "Nam Sausage (M)" does not mark size S."""
    words = set(re.findall(r"[a-z0-9]+", item.lower()))
    return any(want and want <= words
               for want in (set(re.findall(r"[a-z0-9]+", lead.lower())) for lead in leads))


def build_plan_card(turn: list, leads: list, rep: str = "", angles: list | None = None,
                    evidence: str = "") -> dict | None:
    """A visit plan as a screen card, from this turn's lookups.

    "For this visit" is the angles the model chose for this store, in its
    order, each with its items' full facts filled in here from the rows -- the
    model names the angle and the items, never the figures. Everything else in
    the plan follows under "More ideas", so nothing the voice left out is lost
    but the store's own priorities come first, not a fixed layout."""
    plan, checks = None, []
    for message in turn:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("ideas") is not None and payload.get("store"):
            plan = payload
        elif isinstance(payload, dict) and "matches" in payload and payload.get("matches"):
            checks.append(payload)
    if not plan:
        return None
    with _plan_lock:
        cached = _plan_cache.get((plan["store"].get("customer_key"), rep))
    by_title = {title: name for name, title in PLAN_SECTIONS}
    plan = dict(plan, sections=cached["sections"] if cached else
                {by_title.get(t, t): entries for t, entries in (plan.get("ideas") or {}).items()})

    def family_line(group, lead=False):
        """One product family as a card line -- "Sweet rice · Dragonfly, Apple",
        its strongest member's reason -- with every size and brand under it."""
        members = group["members"]
        return {
            "label": group["family"], "brands": ", ".join(group["brands"]),
            "detail": members[0].get("why") or members[0].get("detail") or "",
            "lead": lead or _is_lead(group["family"], leads) or any(_is_lead(m["item"], leads) for m in members),
            "warn": all(not m.get("can_lead", True) for m in members),
            # "case of 4 · $46" tells apart items whose names are the same
            "children": [{"label": display_name(m["item"]) + (" (%s)" % m["pack"] if m.get("pack") else ""),
                          "detail": m.get("detail") or m.get("why") or "",
                          "warn": not m.get("can_lead", True)} for m in members],
        }

    # Every section, by family; the model's checked holiday ideas with them.
    families = [(name, g) for name, es in plan["sections"].items() for g in by_family(es)]
    families += [("occasion", g) for c in checks for g in by_family(c["matches"])]

    # The model's angles: each name it gave found as a family (or a member of
    # one) in any section. A title with a figure the data does not hold falls
    # back to its first family's section.
    featured = []
    seen = set()        # across angles: the steamer came up under two of them
    for angle in angles or []:
        picked = []
        for wanted in angle["items"]:
            hit = next(((n, g) for n, g in families
                        if _is_lead(g["family"], [wanted]) or _is_lead(wanted, [g["family"]])
                        or any(_is_lead(m["item"], [wanted]) for m in g["members"])), None)
            if hit and hit[1]["family"] not in seen:
                seen.add(hit[1]["family"])
                picked.append(hit)
        if not picked:
            continue
        title = angle["angle"]
        if evidence and unsupported_facts(title, evidence):
            title = dict(PLAN_SECTIONS).get(picked[0][0], title)
        featured.append({"title": title, "items": [family_line(g, lead=True) for _, g in picked]})

    sections = []
    occasion = {}
    for entry in plan["sections"].get("occasion", []):
        occasion.setdefault((entry.get("holiday") or "Upcoming", entry.get("holiday_date") or ""), {}).setdefault(
            entry.get("dish") or "", []).append(entry)
    for check in checks:
        group = occasion.setdefault((check.get("occasion") or "Ideas checked", check.get("occasion_date") or ""), {})
        group.setdefault(check.get("dish") or check.get("checked") or "", []).extend(check["matches"])
    for (holiday, when), dishes in occasion.items():
        title = "%s%s" % (holiday, (" (%s)" % _day(when)) if when else "")
        sections.append({"title": title, "groups": [
            {"title": d, "items": [family_line(g) for g in by_family(es)]} for d, es in dishes.items()]})
    for name, title in PLAN_SECTIONS:
        if name != "occasion" and plan["sections"].get(name):
            sections.append({"title": title,
                             "items": [family_line(g) for g in by_family(plan["sections"][name])]})
    store = plan["store"]
    # Only this store's own holidays, on the dates its plan uses -- not every
    # community's calendar, and not the calendar date where its city has one.
    mine = sorted({(when, holiday) for (holiday, when) in occasion if when})
    upcoming = "; ".join("%s %s" % (holiday, _day(when)) for when, holiday in mine)
    return {"type": "plan", "source": "lookup_visit_plan",
            "title": "Visit plan: %s, %s" % (store.get("name"), store.get("city")),
            "note": ("This store's holidays coming up: " + upcoming) if upcoming else "",
            "featured": featured,
            "sections": sections}


# ------------------------------------------------------------------ rep feedback
#
# A thumbs up or down under each answer, with reasons and a comment for a thumbs
# down. Saved with everything needed to see what the rep saw: the question, the
# answer as spoken and as shown, the card, and which lookups ran -- so a bad
# answer can be traced, and a rep's correction ("Pink Lotus is jasmine rice")
# becomes a change to the reference lists. Written to Databricks in the
# background; if that fails, to a local file, so nothing a rep said is lost.

FEEDBACK_TABLE = os.environ.get(
    "FEEDBACK_TABLE", "ust_databricks.ust_reporting.voice_agent_feedback").strip()
FEEDBACK_FILE = ROOT / "feedback.jsonl"
FEEDBACK_MAX_BYTES = 96 * 1024
FEEDBACK_REASONS = ("Wrong info", "Not useful", "Missed something", "Too long", "Wrong store or item",
                    "Useful idea", "Accurate", "Good timing")
_feedback_table_ready = False
_feedback_lock = threading.Lock()


def _clip(value, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value) if value is not None else ""
    return text[:limit]


def feedback_record(body: dict, user: str) -> dict:
    call = _clip(body.get("call"), 64)
    spoken = _clip(body.get("spoken"), 4000)
    # Which lookups produced this answer, from the call's own record of them.
    lookups = []
    with _live_lock:
        entry = _live_calls.get(call) if call else None
        kept = list((entry or {}).get("evidence") or [])
    said = _flat(spoken)
    for item in kept:
        if item["key"] and item["key"][:40] in said:
            for m in item["messages"]:
                for c in m.get("tool_calls") or []:
                    fn = c.get("function") or {}
                    lookups.append("%s(%s)" % (fn.get("name"), _clip(fn.get("arguments"), 120)))
            break
    reasons = [r for r in (body.get("reasons") or []) if r in FEEDBACK_REASONS]
    card = body.get("card")
    store = ""
    if isinstance(card, list) and card and isinstance(card[0], dict):
        store = str(card[0].get("title") or "").replace("Visit plan: ", "")
    return {
        "feedback_id": _clip(body.get("id"), 64) or secrets.token_hex(8),
        "user_name": user, "rep": _clip(body.get("rep"), 120), "call_id": call,
        "conversation_id": _clip(body.get("conversation"), 64),
        "rating": body["rating"], "reasons": ", ".join(reasons), "comment": _clip(body.get("comment"), 1000),
        "question": _clip(body.get("question"), 2000), "answer_spoken": spoken,
        "answer_screen": _clip(body.get("screen"), 6000), "card_json": _clip(card, 60000),
        "store": store[:160], "lookups": "; ".join(lookups)[:4000],
        "client_time": _clip(body.get("at"), 40),
        "input_mode": body.get("mode") if body.get("mode") in ("none", "text", "dictated", "spoken") else "none",
    }


def save_feedback(record: dict) -> None:
    global _feedback_table_ready
    sys.stderr.write("  FEEDBACK %s from %s%s\n" % (record["rating"], record["user_name"],
                                                    (" (%s)" % record["reasons"]) if record["reasons"] else ""))
    columns = list(record)
    try:
        with _feedback_lock:
            if not _feedback_table_ready:
                dbx_records("CREATE TABLE IF NOT EXISTS %s (created_at TIMESTAMP, %s) COMMENT 'Reps rating the "
                            "answers of the voice agent, one row per rating (a changed rating is a new row with "
                            "the same feedback_id; the latest counts). Written by server.py.'"
                            % (FEEDBACK_TABLE, ", ".join("%s STRING" % c for c in columns)), {})
                # A column added since the table was made (input_mode) goes on the end.
                have = {r.get("col_name") for r in dbx_records("DESCRIBE TABLE %s" % FEEDBACK_TABLE, {})}
                missing = [c for c in columns if c not in have]
                if missing:
                    dbx_records("ALTER TABLE %s ADD COLUMNS (%s)"
                                % (FEEDBACK_TABLE, ", ".join("%s STRING" % c for c in missing)), {})
                _feedback_table_ready = True
        dbx_records("INSERT INTO %s (created_at, %s) VALUES (current_timestamp(), %s)"
                    % (FEEDBACK_TABLE, ", ".join(columns), ", ".join(":" + c for c in columns)), record)
    except (common.Fail, OSError) as exc:
        sys.stderr.write("  FEEDBACK not saved to Databricks (%s); kept in %s\n" % (exc, FEEDBACK_FILE.name))
        with _feedback_lock, FEEDBACK_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(record, created_at=time.strftime("%Y-%m-%dT%H:%M:%S"))) + "\n")


SAVE_FEEDBACK_TOOL = {
    "type": "function",
    "function": {
        "name": "save_feedback",
        "description": (
            "Save the rep's feedback on your previous answer, good or bad. Use it when the "
            "rep says \"feedback\", or tells you an answer was useful, spot on, wrong, missing "
            "something, too long -- or corrects you (\"Pink Lotus is jasmine rice\"). Use "
            "their own words for the comment. Afterwards thank them in a few words; do not "
            "repeat the feedback back, and do not apologise at length."),
        "parameters": {"type": "object", "required": ["rating", "comment"], "properties": {
            "rating": {"type": "string", "enum": ["up", "down"],
                       "description": "up if they liked the answer, down if not."},
            "comment": {"type": "string", "description": "What they said, in their words."},
        }},
    },
}


def save_spoken_feedback(args: dict, history: list, rep: str, call: str) -> str:
    """Feedback said to the agent during a call, saved against the answer it
    is about: the last reply before this turn, and the question that got it."""
    rating = args.get("rating") if args.get("rating") in ("up", "down") else "down"
    answer, question = "", ""
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip():
            answer = m["content"]
            for j in range(i - 1, -1, -1):
                if history[j].get("role") == "user" and isinstance(history[j].get("content"), str):
                    question = history[j]["content"]
                    break
            break
    record = feedback_record({"rating": rating, "comment": args.get("comment"), "question": question,
                              "spoken": answer, "screen": answer, "rep": rep, "call": call,
                              "mode": "spoken"}, "voice: " + (rep or "unknown"))
    threading.Thread(target=save_feedback, args=(record,), daemon=True).start()
    return json.dumps({"saved": True, "note": "Thank the rep in a few words and carry on."})


def _num_or_none(value):
    try:
        return _num(value)
    except (TypeError, ValueError):
        return None


def take_visuals(answer: str, history: list) -> tuple[str, list]:
    """The answer without its visual blocks, and the visuals they describe.

    Any fenced block goes, visual or not: code read aloud is never right.
    """
    found = []

    def keep(raw: str) -> None:
        try:
            spec = json.loads(raw.strip())
        except ValueError:
            sys.stderr.write("  VISUAL dropped: not JSON\n")
            return
        if not isinstance(spec, dict) or "type" not in spec:
            return
        visual, why = build_visual(spec, history)
        if visual:
            found.append(visual)
        else:
            sys.stderr.write("  VISUAL %s dropped: %s\n" % (spec.get("type"), why))

    def fenced(match):
        keep(match.group(2))
        return ""

    def bare(match):
        keep(match.group(0))
        return ""

    text = _VISUAL_FENCE.sub(fenced, answer)
    text = _VISUAL_BARE.sub(bare, text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not found and _CLAIMS_SCREEN.search(text):
        # "I've put a breakdown on your screen" with no block written, or one
        # that could not be built: the rep would look for a picture that is
        # not there. The lookup's own suggestion makes the sentence true.
        for message in reversed(history):
            if message.get("role") != "tool":
                continue
            try:
                block = json.loads(message.get("content") or "{}").get("show_on_screen") or ""
            except (ValueError, AttributeError):
                continue
            inner = _VISUAL_FENCE.search(block)
            if inner:
                sys.stderr.write("  VISUAL claimed but not written: using the lookup's suggestion\n")
                keep(inner.group(2))
                break
    return text, found[:1]


_CLAIMS_SCREEN = re.compile(r"\b(on (your|the) screen|on a map|on the map)\b", re.I)


def last_user_text(messages: list) -> str:
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):   # OpenAI content parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content if isinstance(content, str) else ""
    return ""


def tool_specs() -> list[dict]:
    """databricks_tools.json, loaded once. A missing/broken file disables tool
    calls but not the LLM proxy -- plain questions still get answered."""
    global _tool_specs_cache
    if _tool_specs_cache is None:
        try:
            _tool_specs_cache = common.load_specs()
        except common.Fail as exc:
            sys.stderr.write("  LLM proxy: %s -- tool calls disabled\n" % exc)
            _tool_specs_cache = []
    return _tool_specs_cache


def openai_tool_schema(spec: dict) -> dict:
    """One databricks_tools.json entry as an OpenAI function-calling tool --
    the same information tool_payload() in databricks_tool.py sends ElevenLabs,
    just in the other dialect: a single string argument named `value`."""
    label = spec.get("value_label") or spec.get("lookup_column") or "value"
    hint = "The exact %s to search for." % label
    if spec.get("example"):
        hint += " For example %s." % spec["example"]
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": {
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "string", "description": hint}},
            },
        },
    }


def run_tool_call(name: str, value: str, rep: str = "") -> str:
    """Run one pinned Databricks lookup and return its rows as JSON text.

    `rep` limits the lookup to that rep's accounts. It comes from a signed
    token, never from the model: the model picks which tool to run and what to
    search for, and has no say in whose data it searches. A rep asking about a
    colleague's store gets an empty result, the same shape a genuinely unknown
    store returns, so there is nothing to infer from the difference.
    """
    try:
        spec = common.find_spec(tool_specs(), name)
        # A tool keyed on the rep does not need the model to tell us who that
        # is -- the scope token already did, and asking twice only creates a
        # way to disagree. It duly disagreed: reading the rep out of the system
        # prompt, the model passed the unsubstituted "{{rep_name}}" through as
        # a name and every one of those lookups came back empty.
        if (spec.get("lookup_column") or "").endswith("rep_name") and not spec.get("rep_column"):
            value = rep or value
        result = common.run_sql(spec, value, rep)
        state = (result.get("status") or {}).get("state")
        if state != "SUCCEEDED":
            return json.dumps({"error": "Databricks returned %s" % state})
        cols, rows = common.rows_of(result)

        # Nothing found, on a lookup that was narrowed to one rep. Ask again
        # without the narrowing to tell the two cases apart: a store that does
        # not exist, and one that does but is somebody else's. They want
        # different answers -- "I can't find that" sends a rep hunting for a
        # spelling mistake that isn't there, when what they need to hear is
        # whose account it is so they can hand it over.
        #
        # This does confirm the account exists, which silence did not. Among
        # colleagues that is the better trade; it would not be if the audience
        # were wider.
        if not rows and rep and spec.get("rep_column"):
            wider = common.run_sql(spec, value)
            if (wider.get("status") or {}).get("state") == "SUCCEEDED":
                other_cols, other_rows = common.rows_of(wider)
                # Only an owned row is somebody else's. Over a third of the
                # account rows carry no rep at all, and there are key-only
                # rows with every other column null; both match "exists" while
                # belonging to nobody, and calling those another rep's account
                # would be a confident answer about a store that isn't one.
                owner = ""
                if other_rows and spec["rep_column"] in other_cols:
                    owner = other_rows[0][other_cols.index(spec["rep_column"])] or ""
                if other_rows and owner:
                    return json.dumps({
                        "error": "not_your_account",
                        "owner_rep": owner,
                        "message": ("This exists but belongs to another rep, so it is "
                                    "outside what %s can see. Say so plainly and name "
                                    "the rep who owns it if one is given." % (rep or "you")),
                    })
        records = labelled_rows(cols, rows)
        reader = ROW_READERS.get(name)
        if reader:
            for record in records:
                try:
                    record["in_words"] = reader(record)
                except (TypeError, ValueError, KeyError):
                    pass        # leave the row as it is rather than fail the lookup
        if name == "lookup_rep_sales":
            add_vs_last_month(records)
            name_periods(records)
        payload = {"row_count": len(records), "rows": records}
        if name == "lookup_accounts_on_route" and records:
            first = records[0]
            route = road_route(first.get("from_lat"), first.get("from_lon"),
                               first.get("to_lat"), first.get("to_lon"))
            if route:
                payload["drive"] = drive_words(route, first.get("from_place") or "the start",
                                               first.get("to_place") or "the destination")
        # A ready-made visual block for these rows, next to them. Rules in the
        # prompt alone got a bar chart for a trip and nothing for a pitch list;
        # a block to copy is much harder to get wrong. The model still chooses
        # whether to use it, and may change it to fit the question.
        if records and spec.get("visual"):
            block = dict(spec["visual"], **{"from": name})
            payload["show_on_screen"] = "```visual\n%s\n```" % json.dumps(block)
        return json.dumps(payload)
    except common.Fail as exc:
        return json.dumps({"error": str(exc)})


def _num(value):
    return None if value in (None, "") else float(value)


def _money(value) -> str:
    return "${:,.0f}".format(_num(value))


def _pct(value, places: int | None = None) -> str:
    """0.0674 -> '6.7%', 1.2485 -> '125%'; `places` fixes the decimals."""
    v = _num(value) * 100
    if places is not None:
        return "%.*f%%" % (places, v)
    return ("%.1f%%" % v) if abs(v) < 10 else ("%.0f%%" % v)


def read_rep_sales(r: dict) -> str:
    """One period row of lookup_rep_sales as the sentence it should become.

    Left to the raw columns, the model compared the wrong things: October's
    first week against all of September, and last year's change read out as
    the change from last month -- in different ways on different runs, however
    the prompt put it. Working the comparison out here, where it is arithmetic,
    leaves the model a sentence to repeat rather than a table to interpret.
    """
    import datetime
    kind = r["period_type"]
    start = datetime.date.fromisoformat(r["period_start"])
    current = str(r.get("is_current")).lower() == "true"
    names = {"day": "%s %d" % (start.strftime("%b"), start.day),
             "week": "the week of %s %d" % (start.strftime("%b"), start.day),
             "month": start.strftime("%B %Y"),
             "quarter": "Q%d %d" % ((start.month - 1) // 3 + 1, start.year),
             "year": str(start.year)}
    label = names.get(kind, kind)
    as_of_day = datetime.date.fromisoformat(r["as_of_date"])
    as_of = "%s %d" % (as_of_day.strftime("%b"), as_of_day.day)
    parts = []
    has_target = str(r.get("has_target")).lower() == "true" and _num(r.get("period_target"))
    sales = _money(r["sales_amount"])
    if current:
        head = (("%s, the latest day posted: %s sold" % (label, sales)) if kind == "day" else
                ("%s so far (as of %s): %s sold" % (label, as_of, sales)))
        if has_target:
            pace = _num(r["pct_of_target_to_date"])
            if pace is not None:
                gap = abs(pace - 1) * 100
                verdict = ("right on pace" if gap < 0.5 else
                           "%s %s pace" % (_pct(abs(pace - 1)), "ahead of" if pace > 1 else "behind"))
                head += ", %s against a %s target" % (verdict, _money(r["period_target"]))
            if _num(r.get("sales_projected")) is not None and kind != "day":
                head += "; projected to finish at %s, %s of target" % (
                    _money(r["sales_projected"]), _pct(r["pct_of_target_projected"]))
            needed = _num(r.get("sales_still_needed"))
            if needed is not None and kind != "day":
                head += ("; %s still needed with %s selling days left" % (_money(needed), r.get("target_days_remaining"))
                         if needed > 0 else "; already %s over target" % _money(-needed))
        else:
            head += ", with no target set for this period"
        parts.append(head)
    else:
        head = "%s, finished: %s sold" % (label, sales)
        if has_target:
            done = _num(r["pct_of_target_to_date"])
            over = done - 1
            head += " against a %s target, %s" % (
                _money(r["period_target"]),
                "right on target" if abs(over) < 0.005 else
                "%s %s target" % (_pct(abs(over)), "over" if over > 0 else "under"))
        parts.append(head)
    change = _num(r.get("change_vs_same_days_last_year"))
    parts.append("compared with the same days a year earlier (%s): %s" % (
        _money(r["last_year_same_days_sales"]) if _num(r.get("last_year_same_days_sales")) is not None else "n/a",
        "no figure for last year" if change is None else
        "%s %s" % (_pct(abs(change)), "up" if change > 0 else "down")))
    if _num(r.get("margin_pct")) is not None:
        parts.append("margin %s%s, own-brand share %s%s" % (
            _pct(r["margin_pct"], 1), " (under the 13% line)" if str(r.get("margin_below_target")).lower() == "true" else "",
            _pct(r["brand_pct_of_sales"], 1), " (under the 60% line)" if str(r.get("brand_below_target")).lower() == "true" else "")
            + " -- what has posted so far; margin and brand share have no projection, so never offer one")
    return "; ".join(parts) + "."


def add_vs_last_month(records: list) -> None:
    """Give the current month a ready-made comparison with last month.

    "Compare me with last month" spans two rows, a month in progress and a
    finished one, and the model kept joining them wrongly -- a week of
    October against all of September, or last year's change read out as the
    change from last month. The like-for-like comparison is how each stands
    against its own target, so that is what is written here.
    """
    import datetime
    months = [r for r in records if r.get("period_type") == "month"]
    now = next((r for r in months if str(r.get("is_current")).lower() == "true"), None)
    last = next((r for r in months if str(r.get("is_current")).lower() != "true"), None)
    if not now or not last:
        return
    try:
        name_now = datetime.date.fromisoformat(now["period_start"]).strftime("%B")
        name_last = datetime.date.fromisoformat(last["period_start"]).strftime("%B")
        finished, pace = _num(last["pct_of_target_to_date"]), _num(now["pct_of_target_to_date"])
        if finished is None or pace is None:
            return
        def side(v, done):
            gap = abs(v - 1)
            if gap < 0.005:
                return "right on target" if done else "right on pace"
            if done:
                return "%s %s target" % (_pct(gap), "over" if v > 1 else "under")
            return "%s %s pace" % (_pct(gap), "ahead of" if v > 1 else "behind")
        text = ("%s finished %s (%s sold); %s is %s so far" %
                (name_last, side(finished, True), _money(last["sales_amount"]), name_now, side(pace, False)))
        if _num(now.get("sales_projected")) is not None:
            text += ", projected to finish at %s against %s's %s" % (
                _money(now["sales_projected"]), name_last, _money(last["sales_amount"]))
        now["vs_last_month"] = text + ". There is no percentage change from last month: do not compare " \
                                      "this month's sales so far with last month's total."
    except (TypeError, ValueError, KeyError):
        pass


def name_periods(records: list) -> None:
    """A name for each sales period: "October", not "month" twice over.

    The rows are told apart by period_type and period_start together, which is
    fine for reading and no use as a chart label.
    """
    import datetime
    for r in records:
        try:
            start = datetime.date.fromisoformat(r.get("period_start") or "")
        except ValueError:
            continue
        current = str(r.get("is_current")).lower() == "true"
        kind = r.get("period_type")
        if kind == "day":
            r["period"] = "Today" if current else start.strftime("%b %d")
        elif kind == "week":
            r["period"] = "This week" if current else "Week of " + start.strftime("%b %d")
        elif kind == "month":
            r["period"] = start.strftime("%B")
        elif kind == "quarter":
            r["period"] = "Q%d" % ((start.month - 1) // 3 + 1)
        elif kind == "year":
            r["period"] = str(start.year)


def read_account_rhythm(r: dict) -> str:
    """One account from the call-planning lookups, as the sentence to say.

    The point of these lookups is the account's own rhythm -- "usually every 30
    days, it's been 54" -- and that is a ratio the model would otherwise have to
    rebuild from three columns, the kind of arithmetic it has been getting wrong.
    """
    name = re.sub(r"\bLlc\b", "LLC", (r.get("customer_name") or "").title())
    city = (r.get("city") or "").title()
    place = ", ".join(x for x in (city, (r.get("state") or "").upper()) if x)
    head = "%s in %s" % (name, place) if place else name
    if r.get("address"):
        # Without the street a rep planning a stop has to look it up again.
        head += ", %s" % r["address"].title()
    days12 = int(_num(r.get("invoice_days_12m")) or 0)
    gap, since, past = _num(r.get("usual_gap_days")), _num(r.get("days_since_last_invoice")), _num(r.get("days_past_usual"))
    all_time = _num(r.get("invoice_days_all_time"))
    status = (r.get("rhythm_status") or "").lower()
    if all_time and since is not None and status in ("former regular", "dormant"):
        # The win-back list: a store that bought often and stopped. "Buys
        # occasionally" would describe its last year, not what it was.
        body = "a former regular -- bought on %d days in all; last invoice %d days ago" % (all_time, since)
        all_time = None
    elif gap is not None and since is not None:
        body = "usually buys about every %d days; last invoice %d days ago" % (gap, since)
        if past is not None:
            body += (", %.1f times its usual gap" % past) if past >= 1 else ", still within its usual gap"
    elif since is not None:
        body = ("buys only occasionally (%d invoice day%s in the last year); last invoice %d days ago"
                % (days12, "" if days12 == 1 else "s", since))
    else:
        body = "no invoices on record"
    if all_time:
        body += "; bought on %d days in all" % all_time
    extras = []
    if r.get("delivery_days"):
        extras.append("takes deliveries %s" % r["delivery_days"])
    if str(r.get("appointment_required")).lower() == "true":
        extras.append("needs an appointment")
    off, start = _num(r.get("miles_off_route")), _num(r.get("miles_from_start"))
    to_city = (r.get("to_place") or "").split(",")[0].strip().lower()
    if off is not None and start is not None and to_city and to_city == city.lower():
        # Stores in the destination itself are not "on the way" -- that is
        # where the rep is going, and saying otherwise misplaces them.
        extras.append("in the city the rep is headed to, not on the way")
    elif off is not None and start is not None:
        extras.append("on the way: about %d miles off the route, %d miles into the drive (as the crow flies)"
                      % (round(off), start))
    return "%s: %s%s." % (head, body, ("; " + "; ".join(extras)) if extras else "")


def _day(value) -> str:
    """'2022-03-28' -> 'Mar 28, 2022'."""
    import datetime
    d = datetime.date.fromisoformat(str(value)[:10])
    return "%s %d, %d" % (d.strftime("%b"), d.day, d.year)


def _price(value) -> str:
    """62.87 -> '$62.87', 76.0 -> '$76'."""
    text = "${:,.2f}".format(_num(value))
    return text[:-3] if text.endswith(".00") else text


def read_pitch_item(r: dict) -> str:
    """One pitch-list item, as the facts a rep would want before raising it.

    The list used to arrive as "reorder due, 685 days overdue" -- for an item
    bought three times, last in 2022. What sells it, or explains why it
    stopped, is when they last bought it, what they paid against today's price,
    and whether we had any when they came to reorder. Dates and differences
    are worked out here; the model only has to choose and say.
    """
    name = _title_case(r.get("item_label") or "")
    pattern = r.get("purchase_pattern") or ""
    times = int(_num(r.get("times_purchased")) or 0)
    unit = (r.get("price_unit") or "").replace("ip", "innerpack")
    each = (" an %s" if unit[:1] in "aeiou" else " a %s") % unit if unit else ""

    if pattern == "suggestion":
        head = "%s -- a suggestion, they have never bought it (%s)" % (name, r.get("opportunity_reason") or "")
    elif pattern == "regular":
        gap = _num(r.get("median_days_between_purchases"))
        head = "%s -- a regular item: bought %d times, usually about every %d days, last on %s" % (
            name, times, gap or 0, _day(r["last_purchase_date"]))
        if (_num(r.get("days_overdue")) or 0) > 0:
            head += ", so due again"
    else:
        head = "%s -- lapsed: bought %d time%s, last on %s" % (
            name, times, "" if times == 1 else "s", _day(r["last_purchase_date"]))

    facts = []
    paid, now = _num(r.get("last_paid_unit_price")), _num(r.get("unit_price"))
    if paid and now:
        pct = _num(r.get("price_change_pct"))
        if abs(now - paid) < 0.01:
            facts.append("same price as they last paid, %s%s" % (_price(now), each))
        elif abs(pct or 0) < 1:
            facts.append("last paid %s%s, %s now (about the same)" % (_price(paid), each, _price(now)))
        else:
            facts.append("last paid %s%s, %s now (%d%% %s)" % (
                _price(paid), each, _price(now), abs(pct or 0), "more" if now > paid else "less"))
    elif now:
        facts.append("%s%s" % (_price(now), each))

    oos, checked = int(_num(r.get("oos_days_when_due")) or 0), int(_num(r.get("stock_days_checked")) or 0)
    if oos:
        facts.append("U.S. Trading was out of stock -- we had none to ship them -- for %d days "
                     "around when it came due (%s to %s)" % (
            oos, _day(r["first_oos_when_due"]), _day(r["last_oos_when_due"])))
    elif pattern != "suggestion" and checked:
        facts.append("we had stock when it came due")
    elif pattern == "lapsed":
        # History starts Oct 2025: silence here is "unknown", not "in stock".
        facts.append("no stock record from when it was due")

    asks = int(_num(r.get("backorder_requests")) or 0)
    if asks:
        facts.append("they asked for it while we were out %d time%s, last on %s" % (
            asks, "" if asks == 1 else "s", _day(r["last_backorder_date"])))

    # Asked "why did they stop?", the model said it had no reason on record
    # one run in two -- with the stockout and the price rise in the same row.
    # Joined up here, the likely reasons are a fact it can repeat.
    reasons = []
    if oos:
        reasons.append("we were out of stock when they came to reorder")
    if asks:
        reasons.append("they asked for it while we were out")
    if paid and now and (_num(r.get("price_change_pct")) or 0) >= 5:
        reasons.append("the price is up %d%% since they last paid" % _num(r["price_change_pct"]))
    if reasons and (pattern == "lapsed" or (_num(r.get("days_overdue")) or 0) > 0):
        facts.append("possible reasons they have not reordered: " + " and ".join(reasons))

    if str(r.get("is_in_stock")).lower() == "true":
        facts.append("in stock now")
    elif str(r.get("stock_is_known")).lower() == "true":
        facts.append("out of stock now")

    if str(r.get("is_on_promotion")).lower() == "true":
        buy, free = _num(r.get("promo_buy_qty")), _num(r.get("promo_free_qty"))
        promo = "on promotion"
        if buy and free:
            promo += ": buy %d get %d free" % (buy, free)
        elif _num(r.get("promo_price")):
            promo += " at %s" % _price(r["promo_price"])
        if r.get("promo_end_date"):
            promo += " until %s" % _day(r["promo_end_date"])
        facts.append(promo)

    return "%s; %s." % (head, "; ".join(facts)) if facts else head + "."


# Lookups whose rows get a plain-language reading added before the model sees
# them -- where the raw columns proved easy to misread.
ROW_READERS = {
    "lookup_pitch_list": read_pitch_item,
    "lookup_rep_sales": read_rep_sales,
    "lookup_accounts_to_call": read_account_rhythm,
    "lookup_winback_accounts": read_account_rhythm,
    "lookup_accounts_in_city": read_account_rhythm,
    "lookup_accounts_on_route": read_account_rhythm,
}


_DECIMAL = re.compile(r"-?\d+\.\d+")


def labelled_rows(cols: list, rows: list) -> list:
    """Rows as name-to-value records rather than bare arrays.

    Databricks returns a list of column names and rows of values in the same
    order, and handing the model that meant it found a number by counting
    positions. With twenty-odd columns and a row per period it miscounted:
    asked about one day, it gave that day's sales and the month's margin, two
    positions apart in neighbouring rows. A label on every value removes the
    counting.

    Trailing zeros go too -- Databricks pads decimals to six places, and
    "1503083.630000" was miscopied as 1,503,183.63.
    """
    out = []
    for row in rows:
        record = {}
        for name, value in zip(cols, row):
            if isinstance(value, str) and _DECIMAL.fullmatch(value):
                value = value.rstrip("0").rstrip(".")
            record[name] = value
        out.append(record)
    return out


def with_today(messages: list) -> list:
    """The messages with today's date added to the system prompt. The model has
    no clock: without this, "this year" and "in three weeks" are guesses, and a
    search for a festival's dates goes out with the year it was trained in."""
    import datetime
    today = datetime.date.today()
    line = "Today is %s, %s %d, %d." % (today.strftime("%A"), today.strftime("%B"),
                                         today.day, today.year)
    out = list(messages)
    if out and out[0].get("role") == "system" and isinstance(out[0].get("content"), str):
        out[0] = dict(out[0], content=out[0]["content"] + "\n\n" + line)
    else:
        out.insert(0, {"role": "system", "content": line})
    return out


def call_upstream_llm(messages: list, model: str, max_tokens: int, tools: list) -> dict:
    """One non-streaming call to the real Gemma/vLLM host."""
    body = {"model": model, "messages": with_today(messages), "max_tokens": max_tokens,
            "stream": False}
    if tools:
        body["tools"] = tools
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if LLM_UPSTREAM_KEY:
        headers["Authorization"] = "Bearer " + LLM_UPSTREAM_KEY
    if LLM_UPSTREAM_USER_AGENT:
        headers["User-Agent"] = LLM_UPSTREAM_USER_AGENT
    req = urllib.request.Request(
        LLM_UPSTREAM_URL + "/chat/completions",
        data=json.dumps(body).encode("utf-8"), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise UpstreamError(exc.code, exc.read().decode("utf-8", "replace")[:500]) from exc
    except urllib.error.URLError as exc:
        raise UpstreamError(502, "Could not reach the LLM host: %s" % exc.reason) from exc


def run_llm_with_tools(messages: list, model: str, max_tokens: int, rep: str = "",
                       before_lookup=None, trace: list | None = None,
                       visuals: list | None = None, call: str = "") -> str:
    """The tool-calling loop that used to run on ElevenLabs' side: ask the
    model, execute anything it asks for ourselves, feed the result back, and
    repeat until it answers in plain text (or we hit the round cap).

    `before_lookup(model_text)` runs ahead of each round of lookups, with any
    words the model wrote alongside its tool call. It is how the caller gets
    something said while Databricks works. Returning False means nobody is
    listening any more, and the loop stops rather than run queries for no one.

    `trace`, if given, receives the tool calls and results this turn added to
    the conversation -- the evidence the answer rests on (see live_remember).

    `visuals`, if given, receives the charts the answer asked for, filled in
    from the lookups (see take_visuals); the answer comes back without them.
    """
    history = list(messages)
    answer = without_tool_markup(_tool_loop(history, model, max_tokens, rep, before_lookup, call))
    answer = fact_checked(answer, history, model, max_tokens, rep)
    if trace is not None:
        trace.extend(history[len(messages):])
    angles = take_angles(answer)
    leads = [name for a in angles for name in a["items"]] or take_leads(answer)
    answer, found = take_visuals(answer, history)
    if leads:
        answer = drop_dangling_intro(answer)
    # A visit plan's card is built from this turn's rows, whatever the reply
    # did or did not ask for, and takes the place of any picture it did ask for.
    card = build_plan_card(history[len(messages):], leads, rep, angles, _evidence_text(history, rep))
    if card and not angles and _plan_shown_before(messages, card):
        # A follow-up that fetched the same store's plan again: the card is on
        # the screen already, and sending it again with no angles only adds a
        # second copy of everything below the answer.
        card = None
    if card:
        found = [card]
        sys.stderr.write("  VISUAL plan: %d angle(s) for this visit, %d more section(s)\n"
                         % (len(card.get("featured") or []), len(card["sections"])))
    if visuals is not None:
        visuals.extend(found)
    return answer


ASK_FOR_START = json.dumps({"error": (
    "The rep has not said where they are starting from, and you do not know where they are. "
    "Do not guess. Ask them: 'Where will you be starting from?' -- then look up the route "
    "with both cities. If they are already there, use lookup_accounts_in_city instead.")})


def unsaid_start(name: str, value: str, history: list) -> bool:
    """True when a route lookup starts from a city the rep never said.

    We have no idea where a rep is, and the model, told to ask, filled the gap
    anyway -- "heading to Madison" went out as "Milwaukee to Madison", the
    starting city lifted from the tool's own example. Asked or not, the start
    has to have come out of the rep's mouth.
    """
    if name != "lookup_accounts_on_route":
        return False
    ends = re.split(r"(?i)\s+to\s+", value.strip())
    start = _place_words(re.sub(r",.*$", "", ends[0]))
    if len(ends) < 2 or not start.strip():
        return True
    said = _place_words(" ".join(m.get("content") or "" for m in history
                                 if m.get("role") == "user" and isinstance(m.get("content"), str)))
    return start not in said


def _place_words(text: str) -> str:
    """Place names compared as words, St/Ft/Mt spelled out -- the rep says
    "St. Louis", the model may write "Saint Louis", and both are the same place."""
    words = " " + " ".join(re.sub(r"[.,]", " ", (text or "").lower()).split()) + " "
    for short, full in ((" st ", " saint "), (" ft ", " fort "), (" mt ", " mount ")):
        words = words.replace(short, full)
    return words


def _tool_loop(history: list, model: str, max_tokens: int, rep: str, before_lookup, call_id: str = "") -> str:
    """run_llm_with_tools' loop, appending every exchange to `history`."""
    # lookup_visit_plan replaces the pitch list for "what should I pitch": its due
    # and win-back sections are the pitch list's own rows. Offering both, the
    # model would reach for the old one-list answer.
    tools = [openai_tool_schema(s) for s in tool_specs() if s["name"] != "lookup_pitch_list"]
    tools += [VISIT_PLAN_TOOL, CHECK_PRODUCT_TOOL, PLAN_ITEM_TOOL, SAVE_FEEDBACK_TOOL]
    if search_key():
        tools.append(WEB_SEARCH_TOOL)

    for _ in range(LLM_PROXY_MAX_ROUNDS):
        completion = call_upstream_llm(history, model, max_tokens, tools)
        message = ((completion.get("choices") or [{}])[0]).get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls:
            answer = (message.get("content") or "").strip()
            if answer:
                return answer
            # Nothing at all. This is the failure that kept killing calls when
            # ElevenLabs ran the loop: the rows come back, the model returns an
            # empty turn, and nobody asks again -- the caller hears silence
            # until a timeout asks if they are still there. Here we can simply
            # ask again, which is the entire reason for running the loop on
            # this side.
            for attempt in range(1, EMPTY_RETRIES + 1):
                sys.stderr.write("  LLM proxy: empty reply, retrying (%d/%d)\n"
                                 % (attempt, EMPTY_RETRIES))
                completion = call_upstream_llm(history, model, max_tokens, tools)
                message = ((completion.get("choices") or [{}])[0]).get("message") or {}
                if message.get("tool_calls"):
                    break            # it wants a tool now; fall through and run it
                answer = (message.get("content") or "").strip()
                if answer:
                    return answer
            calls = message.get("tool_calls") or []
            if not calls:
                sys.stderr.write("  LLM proxy: still empty after %d retries\n"
                                 % EMPTY_RETRIES)
                return ("Sorry, I lost my train of thought there. "
                        "Could you ask me that again?")

        def parsed_call(call):
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                return fn.get("name") or "", ""
            # Single-value tools take "value", but next to check_store_product's
            # customer_key the model sent lookup_visit_plan {customer_key: ...}:
            # read as blank, refused three times, and the call ran out of rounds.
            value = args.get("value") or next(
                (v for v in args.values() if isinstance(v, str) and v.strip()), "")
            return fn.get("name") or "", value

        # "Let me check" before a question back to the rep would be a promise
        # of a lookup that is not going to happen.
        asking = all(unsaid_start(*parsed_call(c), history) for c in calls)
        # Nor before saving feedback: "Let me check. Thanks for that" is not a lookup.
        asking = asking or all(parsed_call(c)[0] == "save_feedback" for c in calls)
        if not asking and before_lookup and before_lookup(message.get("content") or "") is False:
            return ""
        history.append(message)
        for call in calls:
            name, value = parsed_call(call)
            if unsaid_start(name, value, history):
                sys.stderr.write("  TOOL %s(%r) blocked: start city not said\n" % (name, value))
                history.append({"role": "tool", "tool_call_id": call.get("id"), "content": ASK_FOR_START})
                continue
            if name == "web_search":
                result = web_search(value, history, rep)
            elif name == "lookup_visit_plan":
                result = lookup_visit_plan(value, rep)
            elif name == "check_store_product":
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                value = "%s: %s" % (args.get("customer_key"), args.get("product_words"))
                result = check_store_product(args if isinstance(args, dict) else {}, rep)
            elif name == "save_feedback":
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                args = args if isinstance(args, dict) else {}
                value = "%s: %s" % (args.get("rating"), (args.get("comment") or "")[:60])
                result = save_spoken_feedback(args, history, rep, call_id)
            elif name == "lookup_plan_item":
                try:
                    args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                args = args if isinstance(args, dict) else {}
                value = "%s: %s" % (args.get("customer_key"), args.get("item"))
                result = lookup_plan_item(args, rep)
            else:
                result = run_tool_call(name, value, rep)
                if name in SEASON_CHECK_TOOLS and search_key() and not searched(history):
                    result = with_season_check(result)
            # One line per lookup, because when an answer is wrong the first
            # question is always which tool ran and what it was asked for.
            # Rows counted rather than printed: they are customer data, and
            # this goes to a log.
            try:
                parsed = json.loads(result)
                outcome = (parsed.get("error") or "%d row(s)"
                           % len(parsed.get("rows") or parsed.get("results") or parsed.get("matches")
                                 or parsed.get("found")
                                 or [i for s in (parsed.get("ideas") or {}).values() for i in s]))
            except ValueError:
                outcome = "unreadable"
            sys.stderr.write("  TOOL %s(%r) as %s -> %s\n"
                             % (name, value, rep or "All reps", outcome))
            history.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result,
            })

    # Out of rounds with the model still asking for tools. The lookups may well
    # have found everything: "find the store, check the festival, get the
    # pitch list" is three rounds before a word is said. One more turn with no
    # tools on offer makes it answer from what it has.
    sys.stderr.write("  TOOL loop hit the %d-round cap; answering from what it has\n"
                     % LLM_PROXY_MAX_ROUNDS)
    try:
        completion = call_upstream_llm(history, model, max_tokens, [])
        message = ((completion.get("choices") or [{}])[0]).get("message") or {}
        answer = (message.get("content") or "").strip()
        if answer:
            return answer
    except UpstreamError as exc:
        sys.stderr.write("  LLM proxy: last answer failed: %s\n" % exc)
    # Saying the lookup failed is wrong -- they may well have succeeded -- so
    # say what is true.
    return ("I looked that up but could not put an answer together. "
            "Could you ask me a slightly simpler question?")

# The rep list for the picker. Cached, because it changes about as often as
# somebody joins the company and every sign-in would otherwise wake the
# warehouse. Uses the warm-up token, which is read-only and separate from the
# one the proxy above runs lookups with.
_reps_cache: list = []
_reps_error = ""
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
        global _reps_error
        _reps_error = str(exc)
        sys.stderr.write("  REPS lookup failed: %s\n" % exc)
        return _reps_cache
    _reps_error = ""
    with _reps_lock:
        _reps_cache, _reps_fetched = reps, time.time()
    mark_warehouse_ready()
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
    # jsdelivr styles and OpenStreetMap tiles are for the map under trip
    # answers (public/visuals.js). Only tile images come from OSM; the pins are
    # drawn in the page, so no customer data goes there.
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
    "font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: https://tile.openstreetmap.org; "
    "media-src 'self' blob: mediastream:; "
    "connect-src 'self' https://cdn.jsdelivr.net https://api.elevenlabs.io "
    "wss://api.elevenlabs.io https://*.elevenlabs.io wss://*.elevenlabs.io; "
    # 'self', not 'none': /preview.html frames the app at browser and PDA
    # size. Other sites still cannot frame it.
    "frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
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
        self.send_header("X-Frame-Options", "SAMEORIGIN")
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

    def _handle_feedback(self) -> None:
        """A rep's rating of one answer, from the page. Signed-in reps only;
        the cookie is SameSite=Strict, so another site cannot post one."""
        user = self._user()
        if not user:
            self._json(401, {"error": "Sign in first."})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > FEEDBACK_MAX_BYTES:
            self._json(400, {"error": "Bad request."})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("rating") not in ("up", "down"):
            self._json(400, {"error": "Bad request."})
            return
        record = feedback_record(body, user)
        threading.Thread(target=save_feedback, args=(record,), daemon=True).start()
        self._json(200, {"ok": True})

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
            elif _RETURN_PAGE.match(route):
                # Opening /preview.html signed out used to land on the main
                # page after sign-in, which looks like the preview not working.
                self._redirect("/login", [(
                    "Set-Cookie",
                    "%s=%s; Path=/; Max-Age=600; HttpOnly; SameSite=Strict"
                    % (AFTER_LOGIN_COOKIE, route.lstrip("/")))])
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

        # Called by ElevenLabs' backend, not the rep's browser -- machine to
        # machine, so it is authenticated by a bearer secret instead of the
        # session cookie every /api/* route requires.
        if route == "/llm/v1/chat/completions":
            self._handle_llm_proxy()
            return

        if route == "/api/feedback":
            self._handle_feedback()
            return

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
        # Back to the page that sent them here, if it was one of ours.
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            pass
        wanted = "/" + (jar[AFTER_LOGIN_COOKIE].value if AFTER_LOGIN_COOKIE in jar else "")
        target = wanted if _RETURN_PAGE.match(wanted) else "/"
        self._redirect(target, [("Set-Cookie", cookie), (
            "Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % AFTER_LOGIN_COOKIE)])

    def _stream_live(self, call: str) -> None:
        """Server-sent events for one call: the rep's words as the proxy gets them.

        Only the signed-in user who minted the call can read it. Held open until
        the page goes away; a comment every 15 s keeps idle proxies from closing
        it, and doubles as the check that the page is still there.
        """
        with _live_lock:
            entry = _live_calls.get(call)
        if not entry or entry["user"] != self._user():
            self._json(404, {"error": "No such call."})
            return
        # Chunked, like the proxy's own stream. This used to be a bare body
        # ended by closing the connection, which worked on a laptop and never
        # arrived through the office tunnel: a proxy that cannot see where a
        # response ends may hold it until it does, and this one never does.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.close_connection = True

        def chunk(data: bytes) -> bool:
            try:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
                return True
            except OSError:
                return False

        # Something at once, so the stream is visibly open end to end rather
        # than waiting on the first keep-alive.
        if not chunk(b": open\n\n"):
            return
        sent = len(entry["events"])     # history is ElevenLabs' job; only new words
        while time.time() < entry["expires"]:
            with _live_lock:
                if len(entry["events"]) == sent:
                    _live_lock.wait(15)
                fresh = entry["events"][sent:]
                sent = len(entry["events"])
            data = b"".join(("data: %s\n\n" % json.dumps(item)).encode("utf-8") for item in fresh)
            if not chunk(data or b": keep-alive\n\n"):
                return
        chunk(b"")          # the terminating zero-length chunk

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

        if route == "/api/scope":
            # Mint the scope for a conversation. The rep must be one the
            # directory actually lists: without that check the page could ask
            # for a scope naming anything, and the signature would make it
            # look authoritative.
            wanted = (query.get("rep") or [""])[0].strip()
            if wanted and wanted not in {r["name"] for r in sales_reps()}:
                self._json(400, {"error": "Unknown rep"})
                return
            # TODO: once sign-in maps a user to a rep, take the rep from the
            # session instead of the query string. The proxy side does not
            # change -- it already trusts only what this endpoint signed.
            call = live_open(self._user())
            self._json(200, {"scopeToken": auth.issue_scope(wanted, call=call),
                             "rep": wanted, "callId": call})
            return

        if route == "/api/live":
            self._stream_live((query.get("call") or [""])[0])
            return

        if route == "/api/ready":
            # Held open until the warehouse answers, so the page can show
            # "Getting ready" instead of letting the first question be the slow
            # one. A successful rep-list query already proves it is awake.
            awake, detail = wait_for_warehouse()
            self._json(200, {"ready": awake, "detail": detail})
            return

        if route == "/api/reps":
            # Who the caller can claim to be, until real sign-in exists. An
            # empty list is reported with its reason: the picker falling back
            # to "All" on its own looks identical to a company with no reps,
            # and the difference matters to whoever has to fix it.
            reps = sales_reps()
            payload = {"reps": reps}
            if not reps:
                payload["error"] = (_reps_error or
                                    ("DATABRICKS_WARM_TOKEN is not set" if not WARM_READY
                                     else "no active reps returned"))
            self._json(200, payload)
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

    def _handle_llm_proxy(self) -> None:
        """OpenAI-compatible chat completions endpoint for ElevenLabs' custom_llm.

        Runs the whole tool-calling loop here instead of on ElevenLabs' side, so
        the Databricks credential and every query it runs stay on this server.
        ElevenLabs gets back a plain-text answer and never learns a tool exists.
        """
        if not LLM_PROXY_READY:
            self._json(500, {"error": "LLM proxy not configured -- set LLM_UPSTREAM_URL "
                                       "and LLM_PROXY_SECRET in .env"})
            return

        # Read the body first, always. Answering before draining it leaves the
        # JSON sitting in the socket, and on a keep-alive connection the next
        # read takes it for a request line -- a 401 was being followed by a
        # baffling "Bad request syntax" containing the whole prompt.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if 0 < length <= 1_000_000 else b""

        # ElevenLabs sends the stored secret as "Bearer <value>" for a custom
        # LLM, so the value must be the bare token. A webhook tool's secret is
        # the opposite -- there the stored value is the whole header -- and
        # storing this one that way produced "Bearer Bearer ...".
        given = self.headers.get("Authorization", "")
        if given.startswith("Bearer "):
            given = given[len("Bearer "):]
        if not hmac.compare_digest(given, LLM_PROXY_SECRET):
            self._json(401, {"error": "Unauthorized"})
            return

        if not raw:
            self._json(400, {"error": "Bad request"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._json(400, {"error": "Malformed JSON body"})
            return

        model = payload.get("model") or ""
        messages = payload.get("messages") or []

        # Whose accounts this conversation may read. Minted by /api/scope where
        # the signed-in session is known, carried here by ElevenLabs as an
        # extra body field. Anything unsigned, forged or expired is refused
        # rather than waved through -- treating a bad token as "no limit" would
        # turn every failure into full access.
        # ElevenLabs nests whatever the client passed as extra body under
        # `elevenlabs_extra_body` rather than merging it into the request, so
        # look there first. Top level too, because proxy_test.py and anything
        # else calling this endpoint directly has no reason to nest it.
        extra = payload.get("elevenlabs_extra_body")
        extra = extra if isinstance(extra, dict) else {}
        scope_token = extra.get("scope_token") or payload.get("scope_token") or ""
        rep = auth.read_scope(scope_token)
        if rep is None:
            sys.stderr.write("  LLM proxy: refused, scope token missing or invalid; "
                             "body keys = %s\n" % sorted(payload)[:12])
            self._json(403, {"error": "A valid scope token is required."})
            return
        try:
            max_tokens = int(payload.get("max_tokens") or 512)
        except (TypeError, ValueError):
            max_tokens = 512
        if max_tokens < 1:  # ElevenLabs' "unlimited" sentinel is -1; vLLM rejects it.
            max_tokens = 512

        # Open the stream before doing any work. ElevenLabs holds back the
        # caller's own transcript until the model's first byte arrives, so a
        # lookup that takes ten seconds left the rep's words off the screen for
        # ten seconds too. A role-only chunk carries no text to speak, but it
        # is a first byte.
        chunk_id = "chatcmpl-proxy"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

        write_lock = threading.Lock()   # the keep-alive below writes from its own thread

        def event(data: str) -> bool:
            """Write one SSE event as an HTTP chunk; False once the caller has gone."""
            piece = ("data: %s\n\n" % data).encode("utf-8")
            try:
                with write_lock:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                    self.wfile.flush()
                return True
            except OSError:
                return False

        def delta(body: dict, finish=None) -> str:
            return json.dumps({"id": chunk_id, "object": "chat.completion.chunk", "model": model,
                               "choices": [{"index": 0, "delta": body, "finish_reason": finish}]})

        if not event(delta({"role": "assistant"})):
            return
        call = auth.read_call(scope_token)
        seq = live_publish_user_turn(call, last_user_text(messages))

        # Say something the moment a lookup starts. Without it the rep heard
        # seven to fifteen seconds of nothing while Databricks and the model
        # worked -- the webhook tools' pre-tool speech used to fill that gap,
        # and moving the loop here took it away. Once per turn: a lookup that
        # leads to another must not be announced twice.
        #
        # The model's own words go first when it wrote a short line alongside
        # its tool call ("Let me check their pitch list."); they fit the
        # question better than anything fixed here.
        spoken: list = []
        said_at = [0.0]         # when the rep last heard something from this turn
        exchange: list = []     # this turn's lookups, kept for later turns

        def before_lookup(model_text: str) -> bool:
            # Superseded: the rep kept talking and ElevenLabs asked again.
            # Nobody will hear this answer, so stop before querying for it.
            if not live_is_current(call, seq):
                return False
            if spoken:
                return spoken[0]
            line = " ".join(model_text.split())
            if not line or len(line) > 80 or not line.endswith((".", "!")):
                line = "Let me check."
            spoken.append(event(delta({"content": speakable(line) + " "})))
            said_at[0] = time.time()
            if spoken[0]:
                live_publish_reply(call, line + " ", start=True, seq=seq)
            return spoken[0]

        # ElevenLabs gives up on a reply when nothing new arrives for about 15
        # seconds, and the rep hears "Let me check." and then nothing. A slow
        # lookup is enough: served from the office, a two-lookup turn took 14
        # seconds between "Let me check." and the answer. So while a lookup is
        # still running, say a little more every KEEPALIVE_SECS -- only once
        # something has been said, and only a couple of times.
        finished = threading.Event()

        def keep_alive() -> None:
            lines = list(KEEPALIVE_LINES)
            while lines and not finished.wait(0.5):
                # Counted from the last thing said, not from the request.
                if not spoken or not spoken[0] or time.time() - said_at[0] < KEEPALIVE_SECS:
                    continue
                if not live_is_current(call, seq):
                    return
                line = lines.pop(0)
                if finished.is_set() or not event(delta({"content": line + " "})):
                    return
                said_at[0] = time.time()
                live_publish_reply(call, line + " ", start=False, seq=seq)

        threading.Thread(target=keep_alive, daemon=True).start()
        visuals: list = []
        try:
            answer = run_llm_with_tools(with_display_style(with_evidence(call, messages)),
                                        model, max_tokens, rep, before_lookup, exchange, visuals, call)
        except UpstreamError as exc:
            sys.stderr.write("  LLM proxy upstream error: %s\n" % exc.message)
            answer = "I'm sorry, I ran into a problem answering that."
        except Exception as exc:  # noqa: BLE001 - never kill the server on a bad turn
            sys.stderr.write("  LLM proxy error: %s: %s\n" % (type(exc).__name__, exc))
            answer = "I'm sorry, I ran into a problem answering that."

        finished.set()
        # The answer itself still arrives whole: the tool loop has to finish
        # before there is anything true to say.
        # The screen gets the reply as written; the voice gets it without the
        # markdown, which it would otherwise read out or stumble over.
        sent = event(delta({"content": speakable(answer)}))
        if sent:
            live_publish_reply(call, answer, start=not spoken, seq=seq)
            for visual in visuals:
                live_publish_visual(call, visual, seq=seq)
                sys.stderr.write("  VISUAL %s from %s\n" % (visual["type"], visual.get("source")))
            if live_is_current(call, seq):
                live_remember(call, speakable(answer), exchange)
        if sent and event(delta({}, "stop")) and event("[DONE]"):
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass

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


# Where sign-in returns to: one of our own pages, by name, and nothing else --
# a free-form "next" is how a login page becomes an open redirect.
AFTER_LOGIN_COOKIE = "after_login"
_RETURN_PAGE = re.compile(r"^/[A-Za-z0-9_-]+\.html$")


class QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without a traceback for a client that hung up.

    ElevenLabs abandons a request whenever the rep pauses and carries on, and
    each one printed twenty lines of ConnectionAbortedError that buried the
    lines worth reading.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


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
    if WARM_READY:
        found = len(sales_reps())
        print("  Reps    : " + ("%d in the picker" % found if found
                                else "LOOKUP FAILED (%s) -- picker shows only All"
                                     % (_reps_error or "unknown")))
    print("  Warm-up : " + ("warehouse %s, at most every %d min"
                            % (DBX_WAREHOUSE, WARM_EVERY // 60) if WARM_READY
                            else "off (set DATABRICKS_WARM_TOKEN to enable)"))
    print("  LLM proxy: " + ("ready -> %s (%d tool(s))"
                             % (LLM_UPSTREAM_URL, len(tool_specs())) if LLM_PROXY_READY
                             else "NOT CONFIGURED -- set LLM_UPSTREAM_URL and LLM_PROXY_SECRET"))
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
        httpd = QuietServer((args.host, args.port), Handler)
        # The first page to open used to fetch the rep list itself and wait on
        # a cold warehouse while the picker showed only "All reps". Fetching it
        # now usually has it ready before anyone signs in.
        threading.Thread(target=sales_reps, daemon=True, name="reps-prefetch").start()
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

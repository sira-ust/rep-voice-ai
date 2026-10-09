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
    LLM_PROXY_MAX_ROUNDS = max(1, int(os.environ.get("LLM_PROXY_MAX_TOOL_ROUNDS", "3") or 3))
except ValueError:
    LLM_PROXY_MAX_ROUNDS = 3
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
KEEPALIVE_LINES = ("Still checking.", "Almost there.")


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
    for message in messages:
        if message.get("role") == "assistant" and isinstance(message.get("content"), str):
            said = _flat(message["content"])
            for item in kept:
                if item["key"] and item["key"][:40] in said:
                    out.extend(item["messages"])
                    kept.remove(item)
                    break
        out.append(message)
    return out


def live_is_current(call: str, seq: int) -> bool:
    """False once a newer request for the same call has arrived."""
    with _live_lock:
        entry = _live_calls.get(call) if call else None
        return not entry or entry["seq"] == seq


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
- Nothing else: no headings, tables, links, code or emoji.

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
        return json.dumps({"row_count": len(records), "rows": records})
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


# Lookups whose rows get a plain-language reading added before the model sees
# them -- where the raw columns proved easy to misread.
ROW_READERS = {"lookup_rep_sales": read_rep_sales}


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


def call_upstream_llm(messages: list, model: str, max_tokens: int, tools: list) -> dict:
    """One non-streaming call to the real Gemma/vLLM host."""
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
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
                       before_lookup=None, trace: list | None = None) -> str:
    """The tool-calling loop that used to run on ElevenLabs' side: ask the
    model, execute anything it asks for ourselves, feed the result back, and
    repeat until it answers in plain text (or we hit the round cap).

    `before_lookup(model_text)` runs ahead of each round of lookups, with any
    words the model wrote alongside its tool call. It is how the caller gets
    something said while Databricks works. Returning False means nobody is
    listening any more, and the loop stops rather than run queries for no one.

    `trace`, if given, receives the tool calls and results this turn added to
    the conversation -- the evidence the answer rests on (see live_remember).
    """
    history = list(messages)
    answer = _tool_loop(history, model, max_tokens, rep, before_lookup)
    if trace is not None:
        trace.extend(history[len(messages):])
    return answer


def _tool_loop(history: list, model: str, max_tokens: int, rep: str, before_lookup) -> str:
    """run_llm_with_tools' loop, appending every exchange to `history`."""
    tools = [openai_tool_schema(s) for s in tool_specs()]

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

        if before_lookup and before_lookup(message.get("content") or "") is False:
            return ""
        history.append(message)
        for call in calls:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            name, value = fn.get("name") or "", args.get("value") or ""
            result = run_tool_call(name, value, rep)
            # One line per lookup, because when an answer is wrong the first
            # question is always which tool ran and what it was asked for.
            # Rows counted rather than printed: they are customer data, and
            # this goes to a log.
            try:
                parsed = json.loads(result)
                outcome = (parsed.get("error") or "%d row(s)"
                           % len(parsed.get("rows") or []))
            except ValueError:
                outcome = "unreadable"
            sys.stderr.write("  TOOL %s(%r) as %s -> %s\n"
                             % (name, value, rep or "All reps", outcome))
            history.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": result,
            })

    # Out of rounds with the model still asking for tools. Saying the lookup
    # failed is wrong -- they may well have succeeded -- so say what is true.
    sys.stderr.write("  TOOL loop hit the %d-round cap without an answer\n"
                     % LLM_PROXY_MAX_ROUNDS)
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

        # Called by ElevenLabs' backend, not the rep's browser -- machine to
        # machine, so it is authenticated by a bearer secret instead of the
        # session cookie every /api/* route requires.
        if route == "/llm/v1/chat/completions":
            self._handle_llm_proxy()
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
        self._redirect("/", [("Set-Cookie", cookie)])

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
        try:
            answer = run_llm_with_tools(with_display_style(with_evidence(call, messages)),
                                        model, max_tokens, rep, before_lookup, exchange)
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

#!/usr/bin/env python3
"""
Give the ElevenLabs agent read access to Databricks Unity Catalog tables.

Standard library only. No webhook server to host: ElevenLabs calls the Databricks
SQL Statement Execution API directly, authenticated with a token stored as an
ElevenLabs workspace secret.

    python databricks_tool.py                      # what is configured vs attached
    python databricks_tool.py --sample NAME        # real values from a lookup column
    python databricks_tool.py --test NAME VALUE    # run one query, no ElevenLabs
    python databricks_tool.py --bench NAME VALUE   # cold vs warm latency
    python databricks_tool.py --sync               # push every tool to the agent
    python databricks_tool.py --remove NAME        # drop one tool
    python databricks_tool.py --remove-all         # drop them all

Adding a table
--------------
Add an entry to databricks_tools.json and run --sync. Nothing else changes.
--sync is declarative: it creates new tools, updates changed ones, and detaches
any tool it manages that is no longer in the file.

Safety model
------------
Each tool's SQL text and warehouse id are pinned as `constant_value`, so the
model cannot change the query, the table, the columns or the row limit. It
supplies exactly one value, bound as a Databricks query parameter and never
string-formatted into SQL. This is deliberately not text-to-SQL.

Shared connection settings live in .env; per-table settings live in
databricks_tools.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS_FILE = ROOT / "databricks_tools.json"
EL_API = "https://api.elevenlabs.io/v1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) elevenlabs-webui/1.0"
TIMEOUT = 90


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

DBX_HOST = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
DBX_TOKEN = os.environ.get("DATABRICKS_TOKEN", "").strip()
DBX_WAREHOUSE = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
SECRET_NAME = os.environ.get("DATABRICKS_SECRET_NAME", "DATABRICKS_BEARER").strip()
# A cold serverless warehouse has taken 10-15s to answer. At a 10s ceiling that
# surfaced to the caller as "I encountered an error" (HTTP 504).
TOOL_TIMEOUT = int(os.environ.get("DATABRICKS_TOOL_TIMEOUT", "30") or 30)


class Fail(Exception):
    pass


def need(**values) -> None:
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise Fail("Missing in .env: " + ", ".join(missing))


# ------------------------------------------------------------------ specs

def load_specs() -> list[dict]:
    if not TOOLS_FILE.is_file():
        raise Fail("%s not found" % TOOLS_FILE.name)
    try:
        raw = json.loads(TOOLS_FILE.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise Fail("%s is not valid JSON: %s" % (TOOLS_FILE.name, exc)) from exc

    specs = raw.get("tools") if isinstance(raw, dict) else raw
    if not isinstance(specs, list) or not specs:
        raise Fail("%s has no tools" % TOOLS_FILE.name)

    names = set()
    for spec in specs:
        required = ["name", "table", "description"]
        # A rank tool orders by a named mode; it has no lookup column.
        required.append("modes" if spec.get("kind") == "rank" else "lookup_column")
        for field in required:
            if not spec.get(field):
                raise Fail("Tool %r is missing %r" % (spec.get("name", "?"), field))
        if spec["name"] in names:
            raise Fail("Duplicate tool name %r" % spec["name"])
        names.add(spec["name"])
    return specs


def find_spec(specs: list[dict], name: str) -> dict:
    for spec in specs:
        if spec["name"] == name:
            return spec
    raise Fail("No tool named %r. Configured: %s"
               % (name, ", ".join(s["name"] for s in specs)))


def statement(spec: dict) -> str:
    """The one query this tool can ever run.

    match "exact"     -> WHERE col = :lookup_value       (key lookup)
    match "contains"  -> WHERE lower(col) LIKE %value%    (contiguous phrase)
    match "all_words" -> every word of the phrase appears somewhere in col

    Prefer "all_words" for anything a person says aloud. Catalogue names are
    terse and abbreviated, so a contiguous match misses badly: "pad thai
    noodles" never matches "DF FRESH PAD THAI NOODLE" because of the plural.
    all_words splits on whitespace, drops punctuation, and strips one trailing
    "s" per word, so word order, extra words and plurals all stop mattering.

    `latest_week_column` pins the query to the newest period, so a search over a
    weekly-snapshot table returns each product once rather than once per week.
    """
    columns = spec.get("columns") or []
    select = ", ".join(columns) if columns else "*"
    table = spec["table"]

    # kind "rank": the model picks a named mode from an enum instead of supplying
    # a search value. The mode selects which column to order by, through a CASE
    # over the bound parameter -- SQL parameters bind values, never identifiers,
    # so ORDER BY :col is impossible and this is the safe equivalent. Negating
    # the expression flips the direction, so "top" and "lowest" are both modes.
    if spec.get("kind") == "rank":
        modes = spec.get("modes") or {}
        if not modes:
            raise Fail("Tool %r is kind 'rank' but has no modes" % spec["name"])
        branches = " ".join(
            "WHEN '%s' THEN %s" % (name, expr) for name, expr in modes.items())
        order = "CASE :lookup_value %s END ASC NULLS LAST" % branches

        where = []
        period = spec.get("latest_week_column")
        if period:
            where.append("%s = (SELECT MAX(%s) FROM %s)" % (period, period, table))
        sql = "SELECT %s FROM %s" % (select, table)
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql + " ORDER BY %s LIMIT %d" % (order, int(spec.get("limit", 5)))

    column = spec["lookup_column"]
    match = spec.get("match", "exact")

    if match == "all_words":
        # Each word must appear in the name, OR in the name with spaces removed.
        # The second test is what makes "padthai" find "PAD THAI NOODLE" and
        # "pad thai" find "PADTHAI RICE STICK" -- this catalogue spells it both
        # ways, which no amount of model-side normalising can guess.
        where = [
            "forall("
            "split(regexp_replace(lower(trim(:lookup_value)), '[^a-z0-9 ]', ' '), '\\\\s+'),"
            " w -> w = ''"
            " OR lower({c}) LIKE concat('%%', regexp_replace(w, 's$', ''), '%%')"
            " OR replace(lower({c}), ' ', '') LIKE concat('%%', regexp_replace(w, 's$', ''), '%%')"
            ")".format(c=column)
        ]
    elif match == "contains":
        where = ["lower(%s) LIKE lower(concat('%%', :lookup_value, '%%'))" % column]
    else:
        where = ["%s = :lookup_value" % column]

    period = spec.get("latest_week_column")
    if period:
        where.append("%s = (SELECT MAX(%s) FROM %s)" % (period, period, table))

    sql = "SELECT %s FROM %s WHERE %s" % (select, table, " AND ".join(where))
    if spec.get("order_by"):
        sql += " ORDER BY " + spec["order_by"]
    return sql + " LIMIT %d" % int(spec.get("limit", 1))


# ------------------------------------------------------------------ http

def request(url: str, method: str, headers: dict, body: dict | None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    head = {"Accept": "application/json", "User-Agent": UA}
    if data is not None:
        head["Content-Type"] = "application/json"
    head.update(headers)

    req = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:700]
        raise Fail("HTTP %d on %s %s\n    %s" % (exc.code, method, url, detail)) from exc
    except urllib.error.URLError as exc:
        raise Fail("Cannot reach %s: %s" % (url, exc.reason)) from exc


def elevenlabs(path: str, method: str = "GET", body: dict | None = None) -> dict:
    need(ELEVENLABS_API_KEY=EL_KEY)
    return request(EL_API + path, method, {"xi-api-key": EL_KEY}, body)


def databricks(body: dict) -> dict:
    need(DATABRICKS_HOST=DBX_HOST, DATABRICKS_TOKEN=DBX_TOKEN,
         DATABRICKS_WAREHOUSE_ID=DBX_WAREHOUSE)
    return request(DBX_HOST + "/api/2.0/sql/statements/", "POST",
                   {"Authorization": "Bearer " + DBX_TOKEN}, body)


def run_sql(spec: dict, value: str) -> dict:
    return databricks({
        "warehouse_id": DBX_WAREHOUSE,
        "statement": statement(spec),
        "parameters": [{"name": "lookup_value", "value": value, "type": "STRING"}],
        "wait_timeout": "50s",
        "on_wait_timeout": "CANCEL",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    })


def run_adhoc(sql: str) -> dict:
    """One-off SELECT for setup and diagnostics. Not reachable by the agent."""
    return databricks({
        "warehouse_id": DBX_WAREHOUSE,
        "statement": sql,
        "wait_timeout": "50s",
        "on_wait_timeout": "CANCEL",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    })


def rows_of(result: dict) -> tuple[list, list]:
    cols = [c.get("name") for c in
            (((result.get("manifest") or {}).get("schema") or {}).get("columns") or [])]
    return cols, ((result.get("result") or {}).get("data_array") or [])


# ------------------------------------------------------------------ diagnostics

def cmd_test(spec: dict, value: str) -> None:
    print("\n  tool      : %s" % spec["name"])
    print("  statement : %s" % statement(spec))
    print("  :lookup_value = %r\n" % value)

    started = time.time()
    result = run_sql(spec, value)
    elapsed = time.time() - started

    state = (result.get("status") or {}).get("state")
    print("  state     : %s   (%.2fs round trip)" % (state, elapsed))
    if state != "SUCCEEDED":
        raise Fail("Databricks returned %s: %s"
                   % (state, json.dumps((result.get("status") or {}).get("error"))))

    cols, rows = rows_of(result)
    print("  columns   : %s" % cols)
    print("  rows      : %d" % len(rows))
    for row in rows:
        print("      %s" % row)

    if elapsed > TOOL_TIMEOUT:
        print("\n  WARNING: slower than the tool's %ds ceiling -- the caller would"
              " hear an error." % TOOL_TIMEOUT)
    elif elapsed > 2.0:
        print("\n  NOTE: %.1fs. pre_tool_speech covers it, but a warm warehouse"
              " or Lakebase would cut this to tens of ms." % elapsed)
    if not rows:
        print("\n  No rows matched. Try --sample %s for values that exist." % spec["name"])


def cmd_sample(spec: dict, count: int) -> None:
    print("\n  table  : %s" % spec["table"])
    print("  column : %s\n" % spec["lookup_column"])
    sql = ("SELECT %s, COUNT(*) AS rows_for_key FROM %s GROUP BY %s "
           "ORDER BY rows_for_key DESC LIMIT %d"
           % (spec["lookup_column"], spec["table"], spec["lookup_column"], count))
    started = time.time()
    _, rows = rows_of(run_adhoc(sql))
    print("  (%.2fs)  most common %s values:" % (time.time() - started, spec["lookup_column"]))
    for row in rows:
        print("      %-40s %s row(s)" % (row[0], row[1]))
    if rows:
        print('\n  Try:  python databricks_tool.py --test %s "%s"' % (spec["name"], rows[0][0]))


def cmd_bench(spec: dict, value: str, runs: int = 3) -> None:
    print("\n  Benchmarking %s (%d runs)\n" % (spec["name"], runs))
    times = []
    for i in range(runs):
        started = time.time()
        result = run_sql(spec, value)
        elapsed = time.time() - started
        times.append(elapsed)
        _, rows = rows_of(result)
        print("      run %d: %6.2fs  %s  %d row(s)"
              % (i + 1, elapsed, (result.get("status") or {}).get("state"), len(rows)))
    warm = times[1:] or times
    best = min(times)
    print("\n  first (may include warehouse start): %.2fs" % times[0])
    print("  warm average                       : %.2fs" % (sum(warm) / len(warm)))
    print("  best                               : %.2fs" % best)
    if best > 1.5:
        print("\n  Too slow to feel instant. Raise the warehouse auto-stop, or")
        print("  serve this table from a Lakebase synced table instead.")


# ------------------------------------------------------------------ elevenlabs wiring

def ensure_secret() -> str:
    """Store 'Bearer <token>' as a workspace secret; header values take it whole."""
    need(DATABRICKS_TOKEN=DBX_TOKEN)
    value = "Bearer " + DBX_TOKEN
    existing = elevenlabs("/convai/secrets").get("secrets", [])
    match = next((s for s in existing if s.get("name") == SECRET_NAME), None)
    if match:
        elevenlabs("/convai/secrets/" + match["secret_id"], "PATCH",
                   {"type": "update", "name": SECRET_NAME, "value": value})
        return match["secret_id"]
    created = elevenlabs("/convai/secrets", "POST",
                         {"type": "new", "name": SECRET_NAME, "value": value})
    return created["secret_id"]


def tool_payload(spec: dict, secret_id: str) -> dict:
    label = spec.get("value_label") or spec["lookup_column"]
    hint = "The exact %s to search for." % label
    if spec.get("example"):
        hint += " For example %s." % spec["example"]

    return {
        "tool_config": {
            "type": "webhook",
            "name": spec["name"],
            "description": spec["description"],
            "response_timeout_secs": TOOL_TIMEOUT,
            # A warm lookup is ~1.4s and a cold one far worse. Speaking first
            # turns that into conversation rather than silence.
            "pre_tool_speech": "force",
            # Background noise mid-query must not derail the answer after it.
            "interruption_mode": "disable_during_tool_and_turn",
            "api_schema": {
                "url": DBX_HOST + "/api/2.0/sql/statements/",
                "method": "POST",
                "content_type": "application/json",
                "request_headers": {"Authorization": {"secret_id": secret_id}},
                "request_body_schema": {
                    "type": "object",
                    "required": ["warehouse_id", "statement", "parameters",
                                 "wait_timeout", "on_wait_timeout",
                                 "disposition", "format"],
                    "properties": {
                        # Pinned: the model never sees or changes these.
                        "warehouse_id": {"type": "string", "constant_value": DBX_WAREHOUSE},
                        "statement": {"type": "string", "constant_value": statement(spec)},
                        "wait_timeout": {"type": "string", "constant_value": "30s"},
                        "on_wait_timeout": {"type": "string", "constant_value": "CANCEL"},
                        "disposition": {"type": "string", "constant_value": "INLINE"},
                        "format": {"type": "string", "constant_value": "JSON_ARRAY"},
                        # The only model-supplied value.
                        "parameters": {
                            "type": "array",
                            "description": "Always exactly one element, holding the value to look up.",
                            "items": {
                                "type": "object",
                                "required": ["name", "value", "type"],
                                "properties": {
                                    "name": {"type": "string", "constant_value": "lookup_value"},
                                    "type": {"type": "string", "constant_value": "STRING"},
                                    "value": {"type": "string", "description": hint},
                                },
                            },
                        },
                    },
                },
                # Databricks' reply is verbose; show the model only the rows.
                "response_filter": {
                    "mode": "allow",
                    "filters": ["result.data_array",
                                "manifest.schema.columns.name",
                                "status.state"],
                },
            },
        }
    }


def agent_prompt(agent: dict) -> dict:
    return agent.get("conversation_config", {}).get("agent", {}).get("prompt", {}) or {}


def tool_index() -> dict:
    """Every tool on the workspace, keyed by name."""
    out = {}
    for tool in elevenlabs("/convai/tools").get("tools", []):
        name = (tool.get("tool_config") or {}).get("name")
        tid = tool.get("id") or tool.get("tool_id")
        if name and tid:
            out[name] = tid
    return out


def cmd_sync(specs: list[dict]) -> None:
    need(DATABRICKS_HOST=DBX_HOST, DATABRICKS_WAREHOUSE_ID=DBX_WAREHOUSE,
         ELEVENLABS_AGENT_ID=AGENT_ID)

    print("\n  secret    : %s" % SECRET_NAME)
    secret_id = ensure_secret()
    before = tool_index()

    wanted_ids = []
    for spec in specs:
        payload = tool_payload(spec, secret_id)
        if spec["name"] in before:
            tid = before[spec["name"]]
            elevenlabs("/convai/tools/" + tid, "PATCH", payload)
            print("  updated   : %-24s %s" % (spec["name"], tid))
        else:
            created = elevenlabs("/convai/tools", "POST", payload)
            tid = created.get("id") or created.get("tool_id")
            print("  created   : %-24s %s" % (spec["name"], tid))
        wanted_ids.append(tid)

    agent = elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID))
    attached = list(agent_prompt(agent).get("tool_ids") or [])

    # Detach tools this file used to define but no longer does. Anything created
    # elsewhere (not in `before` under a name we manage) is left alone.
    our_names = {s["name"] for s in specs}
    stale = [tid for name, tid in before.items()
             if name not in our_names and tid in attached and name.startswith("lookup_")]

    keep = [t for t in attached if t not in stale]
    for tid in wanted_ids:
        if tid not in keep:
            keep.append(tid)

    if keep != attached:
        elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
                   {"conversation_config": {"agent": {"prompt": {"tool_ids": keep}}}})
    for tid in stale:
        print("  detached  : %s (no longer in %s)" % (tid, TOOLS_FILE.name))

    print("\n  agent now has %d tool(s)\n" % len(keep))
    cmd_list(specs)


def cmd_list(specs: list[dict]) -> None:
    need(ELEVENLABS_AGENT_ID=AGENT_ID)
    agent = elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID))
    attached = agent_prompt(agent).get("tool_ids") or []
    index = tool_index()
    by_id = {v: k for k, v in index.items()}

    print("  agent     : %s" % agent.get("name"))
    print("  configured in %s:" % TOOLS_FILE.name)
    for spec in specs:
        tid = index.get(spec["name"])
        state = "attached" if tid in attached else ("exists, unattached" if tid else "not created")
        print("      %-22s %-18s %s" % (spec["name"], state, spec["table"]))
        print("          %s" % statement(spec)[:104])

    stray = [t for t in attached if by_id.get(t) not in {s["name"] for s in specs}]
    for tid in stray:
        print("      %-22s attached, not in config" % by_id.get(tid, "?"))

    if len(specs) > 3:
        print("\n  NOTE: %d tools ride along in every LLM request. A small model"
              " picks" % len(specs))
        print("        badly when descriptions overlap -- keep them distinct.")
    print("")


def cmd_remove(names: list[str]) -> None:
    need(ELEVENLABS_AGENT_ID=AGENT_ID)
    index = tool_index()
    doomed = [index[n] for n in names if n in index]
    if not doomed:
        print("\n  Nothing to remove.\n")
        return

    agent = elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID))
    attached = list(agent_prompt(agent).get("tool_ids") or [])
    keep = [t for t in attached if t not in doomed]
    if keep != attached:
        elevenlabs("/convai/agents/" + urllib.parse.quote(AGENT_ID), "PATCH",
                   {"conversation_config": {"agent": {"prompt": {"tool_ids": keep}}}})
        print("\n  detached from agent")
    for tid in doomed:
        elevenlabs("/convai/tools/" + tid, "DELETE")
        print("  deleted %s" % tid)
    print("")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wire Databricks tables into the agent as lookup tools.",
        epilog="Add a table by editing %s, then run --sync." % TOOLS_FILE.name)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sync", action="store_true", help="push every configured tool to the agent")
    group.add_argument("--test", nargs=2, metavar=("NAME", "VALUE"), help="run one tool's query")
    group.add_argument("--bench", nargs=2, metavar=("NAME", "VALUE"), help="time one tool's query")
    group.add_argument("--sample", metavar="NAME", help="real values from a lookup column")
    group.add_argument("--remove", metavar="NAME", help="detach and delete one tool")
    group.add_argument("--remove-all", action="store_true", help="detach and delete every configured tool")
    parser.add_argument("--count", type=int, default=8, help="rows for --sample")
    args = parser.parse_args()

    try:
        specs = load_specs()
        if args.sync:
            cmd_sync(specs)
        elif args.test:
            cmd_test(find_spec(specs, args.test[0]), args.test[1])
        elif args.bench:
            cmd_bench(find_spec(specs, args.bench[0]), args.bench[1])
        elif args.sample:
            cmd_sample(find_spec(specs, args.sample), args.count)
        elif args.remove:
            cmd_remove([args.remove])
        elif args.remove_all:
            cmd_remove([s["name"] for s in specs])
        else:
            print("")
            cmd_list(specs)
            print("  --sample NAME to find real values, --sync to push changes.\n")
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

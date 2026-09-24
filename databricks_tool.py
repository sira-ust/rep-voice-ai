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

import common  # loads .env on import

ROOT = Path(__file__).resolve().parent
TOOLS_FILE = ROOT / "databricks_tools.json"
EL_API = "https://api.elevenlabs.io/v1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) elevenlabs-webui/1.0"
TIMEOUT = 90



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

    `filters` are extra WHERE clauses written here and fixed in the query. They
    are never supplied by the model -- the point of pinning the SQL is that the
    model chooses a value, never a predicate -- so they are the place to say
    things like "the current period only" or "active accounts only", which
    several of these tables need before a row means what it appears to mean.
    """
    columns = spec.get("columns") or []
    select = ", ".join(columns) if columns else "*"
    # `from` lets a spec join -- the biggest-order question needs the customer's
    # name, which lives in a different table from the order. `table` stays the
    # one this tool is about, so the drift check and the web guide still have a
    # single table to point at.
    table = spec.get("from") or spec["table"]

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

        where = list(spec.get("filters") or [])
        period = spec.get("latest_week_column")
        if period:
            where.append("%s = (SELECT MAX(%s) FROM %s)"
                         % (period, period, spec["table"]))
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
        where.append("%s = (SELECT MAX(%s) FROM %s)"
                     % (period, period, spec["table"]))
    where.extend(spec.get("filters") or [])

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
    """Store 'Bearer <token>' as a workspace secret; header values take it whole.

    The token is proved against Databricks first. This is the one credential
    that leaves the machine, and once it is at ElevenLabs the only sign it is
    wrong is every lookup failing mid-call -- nothing here would have said so.
    The way it goes wrong is specific: a stale DATABRICKS_TOKEN left in a shell
    overrides .env by design, so a sync run after pasting a fresh token into
    the file can quietly publish the dead one it replaced.
    """
    need(DATABRICKS_TOKEN=DBX_TOKEN)
    try:
        run_adhoc("SELECT 1")
    except Fail as exc:
        raise Fail(
            "The Databricks token does not work, so it was not sent to "
            "ElevenLabs.\n    %s\n"
            "    Check which value is in force -- a shell variable overrides "
            ".env:\n      python common.py --check" % exc) from exc
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
    # Avoid the word "name" in this hint. The parameter object has a field
    # called `name`, and a label like "customer name" led the model to put the
    # store it was looking up there instead of in `value`, overriding a
    # constant and failing the call.
    hint = "The %s to look up. This goes in the 'value' field." % label
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
                            # Both failure modes below are ones gemma4-26b
                            # actually produced: `parameters` sent as a bare
                            # object rather than a list, and the looked-up value
                            # written into `name`, overriding a constant. Each
                            # fails the call outright, so the schema says in
                            # words what the JSON types already say.
                            "description": (
                                "A JSON array -- square brackets -- holding exactly "
                                "one object, like: "
                                "[{\"name\": \"lookup_value\", \"type\": \"STRING\", "
                                "\"value\": \"<what you are looking up>\"}]. "
                                "Never send this as a bare object. Fill in 'value' "
                                "and nothing else: 'name' and 'type' are fixed and "
                                "must be sent exactly as shown. Putting what you are "
                                "looking up into 'name' makes the query fail."),
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
                #
                # total_row_count earns its place. A query matching nothing
                # returns no data_array at all, so the model saw exactly
                # {"status": {"state": "SUCCEEDED"}} -- a success with no
                # contents -- and filled the silence: asked for a store that
                # does not exist, it answered with a city and an owning rep it
                # had invented. An explicit zero is much harder to talk past
                # than an absent key.
                "response_filter": {
                    "mode": "allow",
                    "filters": ["result.data_array",
                                "manifest.total_row_count",
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


def load_tables() -> dict:
    """The `tables` block: what each table holds, and what it cannot answer."""
    raw = json.loads(TOOLS_FILE.read_text(encoding="utf-8-sig"))
    return (raw.get("tables") or {}) if isinstance(raw, dict) else {}


def cmd_check(specs: list[dict]) -> int:
    """Report anything that goes stale when a table is added or changed.

    Adding a table touches more than this file. The web guide is generated
    from it so it keeps itself honest, but the agent's tools live on
    ElevenLabs and the prompt's claims about what the data cannot answer are
    prose -- neither follows automatically, and a wrong claim is worse than a
    missing one. The agent will keep refusing a question the new table just
    made answerable, and nobody will think to look here.
    """
    tables = load_tables()
    problems = []

    print("\n  tools in %s : %d" % (TOOLS_FILE.name, len(specs)))

    # 1. Every table a tool reads must be described, or the guide has a hole.
    used = {s["table"] for s in specs}
    for table in sorted(used):
        if table not in tables:
            problems.append("%s has no entry in the `tables` block, so the web "
                            "guide cannot describe it" % table)
    for table in sorted(set(tables) - used):
        problems.append("`tables` describes %s but no tool reads it" % table)

    # 2. Everything in the file must actually be on the agent.
    if AGENT_ID and EL_KEY:
        live = tool_index()
        attached = set(agent_prompt(elevenlabs(
            "/convai/agents/" + urllib.parse.quote(AGENT_ID))).get("tool_ids") or [])
        for spec in specs:
            tid = live.get(spec["name"])
            if not tid:
                problems.append("%s is in %s but does not exist on ElevenLabs "
                                "-- run --sync" % (spec["name"], TOOLS_FILE.name))
            elif tid not in attached:
                problems.append("%s exists but is not attached to the agent "
                                "-- run --sync" % spec["name"])
        # And the other direction. A tool dropped from this file can stay
        # attached -- the agent goes on offering a lookup nothing here
        # describes, against a table the guide no longer mentions, which is
        # how an answer arrives from a source nobody thought was still wired
        # up. --sync does not always detach these, so name them.
        configured_ids = {live.get(s["name"]) for s in specs}
        by_id = {tid: name for name, tid in live.items()}
        for tid in sorted(attached - configured_ids):
            problems.append("%s is attached to the agent but is not in %s "
                            "-- remove it with --remove %s"
                            % (by_id.get(tid, tid), TOOLS_FILE.name,
                               by_id.get(tid, tid)))
        print("  attached to agent : %d" % len(attached))
    else:
        print("  (skipped the agent check: ELEVENLABS_* not set)")

    # 3. The guide reads these; a tool without them shows up blank.
    for spec in specs:
        for field in ("subject", "sample_questions"):
            if not spec.get(field):
                problems.append("%s has no %r, so the web guide lists it with "
                                "nothing to ask" % (spec["name"], field))

    if problems:
        print("\n  %d problem(s):" % len(problems))
        for p in problems:
            print("    - %s" % p)
    else:
        print("\n  No drift between the file, the agent and the guide.")

    # The judgement call no check can make. Print the claims so they can be
    # read against the prompt rather than remembered.
    print("\n  ---- confirm agent_prompt.md still agrees with these ----")
    for table, meta in sorted(tables.items()):
        not_covered = (meta.get("not_covered") or "").strip()
        if not_covered:
            print("\n  %s" % (meta.get("label") or table))
            print("    cannot answer: %s" % not_covered)
    print("\n  A new table can make a limitation obsolete. agent_prompt.md")
    print("  states these limits in prose under \"What the data cannot tell")
    print("  you\" -- if a table now covers one, remove it there and from")
    print("  agent_greeting.txt, then: python configure_prompt.py --apply\n")
    return 1 if problems else 0


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
    # A sync is exactly when a table was added, so raise the things a sync
    # cannot fix by itself rather than waiting to be asked.
    cmd_check(specs)


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
    group.add_argument("--check", action="store_true",
                       help="report drift between this file, the agent and the prompt")
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
        elif args.check:
            return cmd_check(specs)
        elif args.remove:
            cmd_remove([args.remove])
        elif args.remove_all:
            cmd_remove([s["name"] for s in specs])
        else:
            print("")
            cmd_list(specs)
            print("  --sample NAME to find real values, --sync to push changes,")
            print("  --check to see what a new table left stale.\n")
    except Fail as exc:
        print("\n  ERROR: %s\n" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

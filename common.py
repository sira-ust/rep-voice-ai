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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
# Only the handful of values that differ when working against a copy of the
# agent. An overlay rather than a second .env: duplicating every secret gives
# a stale token somewhere else to hide, which has cost enough time already.
#
# Loaded only when APP_ENV=test is asked for. Loading it whenever the file
# exists would mean checking out main and still driving the test agent -- the
# same mistake inverted, and quieter, because nothing would break.
ENV_TEST_FILE = ROOT / ".env.test"
APP_ENV = os.environ.get("APP_ENV", "").strip().lower()
TOOLS_FILE = ROOT / "databricks_tools.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) elevenlabs-webui/1.0"


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


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    return "%s...%s (%d chars)" % (value[:4], value[-4:], len(value)) if len(value) > 12 \
        else "(set, %d chars)" % len(value)


# Loading on import is what lets a script say `import common` and be done.
load()

DBX_HOST = os.environ.get("DATABRICKS_HOST", "").strip().rstrip("/")
DBX_TOKEN = os.environ.get("DATABRICKS_TOKEN", "").strip()
DBX_WAREHOUSE = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()
DBX_TOOL_TIMEOUT = int(os.environ.get("DATABRICKS_TOOL_TIMEOUT", "30") or 30)
DBX_REQUEST_TIMEOUT = 90


class Fail(Exception):
    pass


def need(**values) -> None:
    """Raise Fail listing every argument that is falsy, named by its keyword."""
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise Fail("Missing in .env: " + ", ".join(missing))


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


# ------------------------------------------------------------------ tool specs
#
# Shared by databricks_tool.py (registers these as ElevenLabs webhook tools) and
# server.py (runs them itself from inside the LLM proxy). One definition, one
# safety model: SQL text and warehouse id are always pinned per spec, the
# caller only ever supplies the single :lookup_value parameter.

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


def rep_clause(spec: dict) -> str:
    """The predicate that limits a lookup to one rep's accounts, or "".

    Bound as a parameter rather than pasted in: the rep arrives from outside
    this process, and a name with an apostrophe in it would otherwise be a
    broken query at best. `rep_column` names the column that says whose account
    a row is -- tools already keyed on the rep need none, and products belong
    to nobody, so both come back empty.
    """
    column = spec.get("rep_column")
    return " AND %s = :rep_name" % column if column else ""


def statement(spec: dict, rep: str = "") -> str:
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
        clause = rep_clause(spec) if rep else ""
        if clause:
            sql += (" WHERE" + clause[4:]) if not where else clause
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
    if rep:
        sql += rep_clause(spec)
    if spec.get("order_by"):
        sql += " ORDER BY " + spec["order_by"]
    return sql + " LIMIT %d" % int(spec.get("limit", 1))


# ------------------------------------------------------------------ Databricks http

def _request(url: str, method: str, headers: dict, body: dict | None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    head = {"Accept": "application/json", "User-Agent": UA}
    if data is not None:
        head["Content-Type"] = "application/json"
    head.update(headers)

    req = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=DBX_REQUEST_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:700]
        raise Fail("HTTP %d on %s %s\n    %s" % (exc.code, method, url, detail)) from exc
    except urllib.error.URLError as exc:
        raise Fail("Cannot reach %s: %s" % (url, exc.reason)) from exc


def databricks(body: dict) -> dict:
    need(DATABRICKS_HOST=DBX_HOST, DATABRICKS_TOKEN=DBX_TOKEN,
         DATABRICKS_WAREHOUSE_ID=DBX_WAREHOUSE)
    return _request(DBX_HOST + "/api/2.0/sql/statements/", "POST",
                     {"Authorization": "Bearer " + DBX_TOKEN}, body)


def run_sql(spec: dict, value: str, rep: str = "") -> dict:
    """Run a pinned lookup. `rep` limits it to that rep's accounts where the
    spec says which column decides that; an empty rep means no limit, which is
    what a manager gets."""
    params = [{"name": "lookup_value", "value": value, "type": "STRING"}]
    if rep and spec.get("rep_column"):
        params.append({"name": "rep_name", "value": rep, "type": "STRING"})
    return databricks({
        "warehouse_id": DBX_WAREHOUSE,
        "statement": statement(spec, rep),
        "parameters": params,
        "wait_timeout": "50s",
        "on_wait_timeout": "CANCEL",
        "disposition": "INLINE",
        "format": "JSON_ARRAY",
    })


def rows_of(result: dict) -> tuple[list, list]:
    cols = [c.get("name") for c in
            (((result.get("manifest") or {}).get("schema") or {}).get("columns") or [])]
    return cols, ((result.get("result") or {}).get("data_array") or [])


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

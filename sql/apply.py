#!/usr/bin/env python3
"""
Create (or replace) the hand-made views in this folder in Databricks.

    python sql/apply.py                              # every .sql file here
    python sql/apply.py sql/mart_account_item_pitch.sql

Each file is one CREATE OR REPLACE statement. These are stopgaps over the dbt
models -- the real home for them is the dbt project -- so they only ever add
views; nothing here alters or drops a dbt table.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # loads .env on import


def apply(path: Path) -> bool:
    statement = path.read_text(encoding="utf-8")
    result = common.databricks({
        "warehouse_id": common.DBX_WAREHOUSE,
        "statement": statement,
        "wait_timeout": "50s",
        # A table build can outlast the 50 s the API will wait in one call;
        # keep it running and poll rather than have it cancelled.
        "on_wait_timeout": "CONTINUE",
    })
    deadline = time.time() + 15 * 60
    while ((result.get("status") or {}).get("state") in ("PENDING", "RUNNING")
           and time.time() < deadline):
        time.sleep(5)
        result = common._request(
            common.DBX_HOST + "/api/2.0/sql/statements/" + result["statement_id"], "GET",
            {"Authorization": "Bearer " + common.DBX_TOKEN}, None)
    status = result.get("status") or {}
    if status.get("state") == "SUCCEEDED":
        print("  applied  %s" % path.name)
        return True
    print("  FAILED   %s: %s" % (path.name, (status.get("error") or {}).get("message", status)))
    return False


def main() -> int:
    here = Path(__file__).resolve().parent
    files = [Path(a) for a in sys.argv[1:]] or sorted(here.glob("*.sql"))
    return 0 if all([apply(f) for f in files]) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Load the holiday reference lists into Databricks.

    python sql/load_holidays.py

Each CSV here is the only copy that matters: edit it (Excel is fine, save as
CSV), then run this again. Each load replaces its whole table.

community_holidays.csv   -> ust_reporting.ref_community_holidays
    Wide: one row per holiday, one column per year. Loaded one row per
    holiday, community and date ("Chinese / Vietnamese" becomes two rows,
    lower-cased). date_varies marks lunar or moon-sighting dates to confirm
    each year. "American" and "All" holidays apply to every store; that is
    decided where the table is read.

holiday_products.csv     -> ust_reporting.ref_holiday_products
    What each holiday calls for, dish by dish, with match_terms: phrases
    separated by |, where every word of a phrase must be in an item's label.

account_communities.csv  -> ust_reporting.ref_account_communities
    Which communities each store serves; a store can have several rows.

holiday_local_dates.csv  -> ust_reporting.ref_holiday_local_dates
    A city's own date for a holiday, used instead of the calendar's for stores
    in that city (Madison's Hmong New Year is Oct 31, the calendar's Dec 1).

The two drafted lists come from sql/draft_holiday_inputs.py; rows marked
source=model, confirmed=no are Gemma's and want a person's check.
"""

from __future__ import annotations

import csv
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # loads .env on import

HERE = Path(__file__).resolve().parent
SCHEMA = "ust_databricks.ust_reporting"
SOURCE = "Source: sql/%s in voiceAI -- edit that and rerun sql/load_holidays.py."


def text(value) -> str:
    return "'" + str(value or "").strip().replace("\\", "\\\\").replace("'", "''") + "'"


def date(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "NULL"
    return "DATE'%s'" % datetime.datetime.strptime(raw, "%m/%d/%Y").date().isoformat()


def read(name: str) -> list[dict]:
    path = HERE / name
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def holidays() -> list[str]:
    out = []
    for line in read("community_holidays.csv"):
        holiday, label = (line.get("holiday") or "").strip(), (line.get("community") or "").strip()
        if not holiday or not label:
            continue
        varies = (line.get("date_varies") or "").strip().lower() in ("yes", "true", "**", "y")
        for year in [k for k in line if k and k.strip().isdigit()]:
            if (line.get(year) or "").strip():
                for community in (c.strip().lower() for c in label.split("/") if c.strip()):
                    out.append("(%s, %s, %s, %s, %s, %s)" % (
                        text(holiday), text(community), text(label), date(line[year]),
                        "true" if varies else "false", text(line.get("note"))))
    return out


def products() -> list[str]:
    return ["(%s, %s, %s, %s, %s, %d, %s, %s, %s)" % (
        text(r["holiday"]), text(r["community"]), text(r["dish"]), text(r["product"]),
        text(r["match_terms"].lower()), int(float(r.get("matched_items") or 0)), text(r.get("why")),
        text(r.get("source")), "true" if (r.get("confirmed") or "").lower() in ("yes", "true") else "false")
        for r in read("holiday_products.csv") if r.get("holiday") and r.get("match_terms")]


def communities() -> list[str]:
    return ["(%s, %s, %s, %s)" % (
        text(r["customer_key"].upper()), text(r["community"].lower()), text(r.get("source")),
        "true" if (r.get("confirmed") or "").lower() in ("yes", "true") else "false")
        for r in read("account_communities.csv") if r.get("customer_key") and r.get("community")]


def local_dates() -> list[str]:
    return ["(%s, %s, %s, %s, %s, %s)" % (
        text(r["holiday"]), text(r["city"]), text(r.get("state")), date(r["start_date"]),
        date(r.get("end_date")), text(r.get("note")))
        for r in read("holiday_local_dates.csv") if r.get("holiday") and r.get("city")]


TABLES = (
    ("ref_community_holidays", "community_holidays.csv", holidays,
     "holiday STRING, community STRING, communities STRING, holiday_date DATE, "
     "date_varies BOOLEAN, note STRING",
     "Community holiday calendar for the voice agent."),
    ("ref_holiday_products", "holiday_products.csv", products,
     "holiday STRING, communities STRING, dish STRING, product STRING, match_terms STRING, "
     "matched_items INT, why STRING, source STRING, confirmed BOOLEAN",
     "What each holiday calls for, dish by dish, for the visit plan's occasion section."),
    ("ref_account_communities", "account_communities.csv", communities,
     "customer_key STRING, community STRING, source STRING, confirmed BOOLEAN",
     "Which communities each store serves, for the visit plan's occasion section."),
    ("ref_holiday_local_dates", "holiday_local_dates.csv", local_dates,
     "holiday STRING, city STRING, state STRING, start_date DATE, end_date DATE, note STRING",
     "A city's own date for a holiday, used instead of the calendar's."),
)


def run(statement: str) -> bool:
    result = common.databricks({"warehouse_id": common.DBX_WAREHOUSE, "statement": statement,
                                "wait_timeout": "50s", "on_wait_timeout": "CANCEL"})
    status = result.get("status") or {}
    if status.get("state") != "SUCCEEDED":
        print("  FAILED: %s" % (status.get("error") or {}).get("message", status))
        return False
    return True


def main() -> int:
    for table, source, rows_of, columns, comment in TABLES:
        values = rows_of()
        if not values and not (HERE / source).is_file():
            print("  skipped  %s (no %s)" % (table, source))
            continue
        full = "%s.%s" % (SCHEMA, table)
        if not run("CREATE OR REPLACE TABLE %s (%s) COMMENT %s" % (full, columns, text(comment + " " + SOURCE % source))):
            return 1
        for start in range(0, len(values), 500):
            if not run("INSERT INTO %s VALUES %s" % (full, ", ".join(values[start:start + 500]))):
                return 1
        print("  loaded   %5d rows into %s" % (len(values), full))
    return 0


if __name__ == "__main__":
    sys.exit(main())

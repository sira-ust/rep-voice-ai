#!/usr/bin/env python3
"""
Draft the two reference lists the visit plan's holiday section needs, with Gemma.

    python sql/draft_holiday_inputs.py products       # -> sql/holiday_products.csv
    python sql/draft_holiday_inputs.py communities    # -> sql/account_communities.csv

These are drafts for people to review, not something to rerun nightly: what a
holiday calls for, and which community a store serves, change rarely. Every
row is written with source=model, confirmed=no. Correct the CSVs (Excel is
fine), then load them with `python sql/load_holidays.py`.

products     For each holiday in community_holidays.csv: the dishes it calls
             for, what goes into each, and the words a catalog label would use
             (match_terms). Each term is checked against our item catalog;
             matched_items says how many items it found, and a term that finds
             nothing is dropped.
communities  For each store with a visit plan: the communities it serves,
             judged from its name, its town and what it buys most, from the
             list the calendar uses. Stores are sent 25 at a time.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import common   # noqa: E402  (loads .env)
import server   # noqa: E402  (the Gemma call and the Databricks helpers)

HERE = Path(__file__).resolve().parent
COMMUNITIES = ("hmong", "lao", "thai", "burmese", "cambodian", "vietnamese", "chinese", "korean",
               "filipino", "indian", "sikh", "malay", "indonesian", "muslim", "mexican", "hispanic")


def gemma(prompt: str) -> str:
    model = server.LLM_MODEL if hasattr(server, "LLM_MODEL") else ""
    import os
    model = model or os.environ.get("CUSTOM_LLM_MODEL", "gemma4-26b")
    completion = server.call_upstream_llm([{"role": "user", "content": prompt}], model, 4000, [])
    return (((completion.get("choices") or [{}])[0]).get("message") or {}).get("content") or ""


def as_json(text: str):
    text = re.sub(r"```(?:json)?", "", text or "")
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    return None


# ------------------------------------------------------------------ products

PRODUCTS_PROMPT = """You are a merchandiser at U.S. Trading Company, a wholesale distributor of Asian
food and grocery supplies to independent grocery stores in the US Midwest.

Holiday: {holiday}
Celebrated by: {communities}

List what stores serving that community stock up on for this holiday: the
dishes people make or serve, and the ingredients and supplies for each that a
grocery wholesaler would sell (not fresh produce or meat a grocer buys
elsewhere unless it is packaged -- frozen, dried, canned, bottled are fine).
Also include non-food items if they are part of it (incense, candles,
red envelopes, cookware).

Give 4 to 6 dishes or uses, and 2 to 4 products for each.

For each product, match_terms: 1 to 3 short phrases separated by |, in the
words a wholesale catalog label would use, e.g. "sweet rice|glutinous rice" or
"rice paper". Every word of a phrase must appear in the label for it to match,
so keep phrases short and plain.

Reply with JSON only, a list:
[{{"dish": "...", "product": "...", "match_terms": "...", "why": "how it is used, one short sentence"}}]"""


def catalog_counts(terms: list[str]) -> dict:
    """How many sellable items each term matches (every word in the label)."""
    out = {}
    for start in range(0, len(terms), 40):
        chunk = terms[start:start + 40]
        selects, params = [], {}
        for i, term in enumerate(chunk):
            words = [w for w in re.findall(r"[a-z0-9]+", term.lower()) if len(w) > 1][:4]
            if not words:
                out[term] = 0
                continue
            conds = []
            for j, w in enumerate(words):
                params["t%d_%d" % (i, j)] = "%" + w + "%"
                conds.append("lower(item_label) LIKE :t%d_%d" % (i, j))
            selects.append("SELECT %d AS i, count(*) AS n FROM ust_databricks.ust_dims.dim_items "
                           "WHERE is_sellable AND NOT is_blocked AND %s" % (i, " AND ".join(conds)))
        if selects:
            for r in server.dbx_records(" UNION ALL ".join(selects), params):
                out[chunk[int(r["i"])]] = int(float(r["n"]))
    return out


def draft_products() -> int:
    with (HERE / "community_holidays.csv").open(encoding="utf-8-sig", newline="") as f:
        holidays = [(r["holiday"].strip(), r["community"].strip()) for r in csv.DictReader(f) if r.get("holiday")]
    rows = []
    for holiday, communities in holidays:
        print("  %-40s" % holiday[:40], end="", flush=True)
        items = as_json(gemma(PRODUCTS_PROMPT.format(holiday=holiday, communities=communities)))
        if not isinstance(items, list):
            print(" -- no usable reply, skipped")
            continue
        drafted = [i for i in items if isinstance(i, dict) and i.get("product") and i.get("match_terms")]
        terms = sorted({t.strip() for i in drafted for t in str(i["match_terms"]).split("|") if t.strip()})
        counts = catalog_counts(terms)
        kept = 0
        for i in drafted:
            good = [t.strip() for t in str(i["match_terms"]).split("|") if counts.get(t.strip())]
            if not good:
                continue
            kept += 1
            rows.append({"holiday": holiday, "community": communities, "dish": i.get("dish", ""),
                         "product": i["product"], "match_terms": "|".join(good),
                         "matched_items": sum(counts[t] for t in good), "why": i.get("why", ""),
                         "source": "model", "confirmed": "no"})
        print(" %d of %d products match our catalog" % (kept, len(drafted)))
    path = HERE / "holiday_products.csv"
    # A rep's rows stand: a redraft replaces only what the model wrote.
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as f:
            kept = [r for r in csv.DictReader(f) if (r.get("source") or "").lower() == "rep"]
        drafted = {(r["holiday"], r["product"].lower()) for r in kept}
        rows = kept + [r for r in rows if (r["holiday"], r["product"].lower()) not in drafted]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("\n  wrote %d rows to %s" % (len(rows), path.name))
    return 0


# ------------------------------------------------------------------ communities

COMMUNITIES_PROMPT = """Below are grocery stores that buy from U.S. Trading Company, a wholesale
distributor of Asian food. For each, judge which communities its shoppers
mostly come from, from the store's name, its town and what it buys most.

Use only these words: {allowed}.
Give one to three per store, most likely first. If you cannot tell, give [].
A general "Asian market" with mixed buying gets the communities its buying
points to, not all of them.

Reply with JSON only, an object of account key to list:
{{"HMO032": ["hmong", "lao"], ...}}

Stores:
{stores}"""


def draft_communities() -> int:
    stores = server.dbx_records("""
        WITH accounts AS (
            SELECT DISTINCT customer_key FROM %s),
        bought AS (
            SELECT p.customer_key, d.item_label, count(DISTINCT p.posting_date) AS days
            FROM ust_databricks.ust_intermediate.int_customer_item_purchases p
            JOIN ust_databricks.ust_dims.dim_items d USING (item_no)
            JOIN accounts a USING (customer_key)
            WHERE p.sold_quantity > 0 AND p.posting_date >= date_sub(current_date(), 365)
            GROUP BY p.customer_key, d.item_label),
        top AS (
            SELECT customer_key, item_label,
                   row_number() OVER (PARTITION BY customer_key ORDER BY days DESC, item_label) AS rn
            FROM bought)
        SELECT a.customer_key, first(c.customer_name) AS customer_name, first(c.city) AS city,
               concat_ws(', ', collect_list(t.item_label)) AS top_items
        FROM accounts a
        JOIN ust_databricks.ust_dims.dim_customers c USING (customer_key)
        LEFT JOIN top t ON t.customer_key = a.customer_key AND t.rn <= 12
        GROUP BY a.customer_key ORDER BY a.customer_key""" % server.VISIT_PLAN_TABLE, {})
    print("  %d stores" % len(stores))
    rows = []
    for start in range(0, len(stores), 25):
        batch = stores[start:start + 25]
        listing = "\n".join("%s | %s | %s | buys: %s" % (
            s["customer_key"], s.get("customer_name") or "", s.get("city") or "",
            (s.get("top_items") or "")[:400]) for s in batch)
        answer = as_json(gemma(COMMUNITIES_PROMPT.format(allowed=", ".join(COMMUNITIES), stores=listing)))
        answer = answer if isinstance(answer, dict) else {}
        for s in batch:
            picks = [c for c in (answer.get(s["customer_key"]) or []) if c in COMMUNITIES][:3]
            for c in picks:
                rows.append({"customer_key": s["customer_key"], "customer_name": s.get("customer_name") or "",
                             "city": s.get("city") or "", "community": c,
                             "source": "model", "confirmed": "no"})
        print("  %4d/%d stores, %d rows" % (min(start + 25, len(stores)), len(stores), len(rows)), flush=True)
    path = HERE / "account_communities.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["customer_key", "customer_name", "city", "community",
                                               "source", "confirmed"])
        writer.writeheader()
        writer.writerows(rows)
    print("\n  wrote %d rows to %s" % (len(rows), path.name))
    return 0


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "products":
        sys.exit(draft_products())
    if what == "communities":
        sys.exit(draft_communities())
    print(__doc__)
    sys.exit(1)

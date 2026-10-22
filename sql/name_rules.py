#!/usr/bin/env python3
"""
Tidy sql/account_communities.csv with store-name rules, after the Gemma draft.

    python sql/name_rules.py

Gemma, judging 25 stores at a time from what they buy, called 1,152 of 1,896
stores Thai -- most stores' top items are DF and Dragonfly, our own Thai-made
brand -- and gave Shwe Market (Burmese) Hmong, Lao and Vietnamese. A store's
name is the strongest clue there is, so where a rule below matches the name,
its communities replace the model's (source=name). Elsewhere only the model's
first pick is kept (source=model). Nothing here is confirmed: reps review the
file and set confirmed=yes, and a rep's correction (source=rep) is never
overwritten by a rerun.
"""

import csv
import re
from pathlib import Path

PATH = Path(__file__).resolve().parent / "account_communities.csv"

# Whole-word patterns on the store name -> communities, most specific first.
RULES = (
    (r"hmong", ["hmong"]),
    (r"xiang|xieng", ["hmong", "lao"]),
    (r"shwe|burm|myanmar|yangon|mandalay|pyone|karen", ["burmese"]),
    (r"viet|saigon|hanoi|pho\b", ["vietnamese"]),
    (r"thai\b|siam|bangkok", ["thai"]),
    (r"\blao\b|laos|vientiane|luang", ["lao"]),
    (r"khmer|cambod|angkor|phnom", ["cambodian"]),
    (r"filipin|pinoy|manila|isabela|phil(ippine)?\b|seafood city", ["filipino"]),
    (r"korea|seoul|h ?mart|hanin", ["korean"]),
    (r"india|desi|patel|bombay|punjab", ["indian"]),
    (r"china|chinese|chinatown|hong kong|canton|mei hua", ["chinese"]),
    (r"mexic|latin|tienda|mercado|carnicer", ["mexican", "hispanic"]),
    (r"indo\b|indonesia|bali\b|jakarta", ["indonesian"]),
)


def main() -> int:
    rows = list(csv.DictReader(PATH.open(encoding="utf-8-sig")))
    by_store, order = {}, []
    for r in rows:
        if r["customer_key"] not in by_store:
            order.append(r["customer_key"])
        by_store.setdefault(r["customer_key"], []).append(r)
    out, named = [], 0
    for key in order:
        group = by_store[key]
        first = group[0]
        if any(r.get("source") == "rep" for r in group):
            out += group                         # a rep's word stands
            continue
        name = (first.get("customer_name") or "").lower()
        hit = next((c for pattern, c in RULES if re.search(pattern, name)), None)
        picks = [(c, "name") for c in hit] if hit else [(group[0]["community"], "model")]
        named += bool(hit)
        out += [{**first, "community": c, "source": src, "confirmed": "no"} for c, src in picks]
    with PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(out)
    print("  %d stores: %d by name, %d by the model's first pick; %d rows"
          % (len(order), named, len(order) - named, len(out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

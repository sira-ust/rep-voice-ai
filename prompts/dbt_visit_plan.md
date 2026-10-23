# Build the nightly "visit plan" models in the dbt project

You are working in U.S. Trading Company's dbt project (Databricks, catalog
`ust_databricks`). Build a set of models that run in the existing nightly
pipeline and produce one table: **`ust_reporting.mart_account_visit_plan`**.
It tells a field sales rep what to raise with each store on a visit, grouped
by reason, with the evidence for each item.

A voice sales assistant (a separate repo, "voiceAI") reads this table. On a
call it speaks a short summary and shows the full plan on the rep's screen.
The heavy work has to be done here, overnight, because the assistant can't
spend 20–30 seconds computing it during a call.

Read the whole brief before writing anything. Then propose the model list,
grain and materializations, and wait for approval before building.

---

## Ground rules

- **Use only modeled tables:** `ust_dims.*`, `ust_facts.*`,
  `ust_intermediate.int_*`, `ust_reporting.mart_*`, and this project's seeds.
  **Never** read the raw source replicas (`navrep`, `mysql`, `jdawmsrep`
  schemas).
- Follow this project's existing conventions for naming, folders, staging
  layers, tests, docs and materializations. Look at how neighbouring models
  are built and match them.
- Every model and column gets a description. The voice assistant's developer
  reads these docs to know what each column means.
- The final mart must finish comfortably inside the nightly window. Report
  its run time.

---

## Inputs that already exist

Verify these columns before relying on them, and report any that differ.

| Model | Columns used |
|---|---|
| `ust_intermediate.int_customer_item_purchases` | customer_key, item_no, posting_date, quantity_uom, sold_quantity, sold_value, returned_quantity, net_quantity |
| `ust_dims.dim_items` | item_no, item_label, item_category_name, unit_price, base_uom, case_qty, is_in_stock, shippable_qty, on_hand_qty, stock_snapshot_date, is_on_promotion, promo_price, promo_buy_qty, promo_free_qty, promo_end_date, is_new_item, is_popular_item, is_discontinued, is_sellable, is_blocked |
| `ust_dims.dim_customers` | customer_key, customer_name, city, post_code, owner_rep_name |
| `ust_intermediate.int_jdawms_inventory_daily` | prtnum (= item_no, string), snapshot_date, shippable_qty. History starts 2025-10-27 |
| `ust_intermediate.int_events_enriched` | customer_key, function_name, response, rep_local_date. A backorder request is `function_name = 'add_item_backorder_page'`, and `response` is `"<item_no>, <qty>"`. Events start Feb 2026 |
| `ust_reporting.mart_account_item_opportunities` | the existing per-account pitch candidates: opportunity_rank, opportunity_reason, is_evidence_based, times_purchased, last_purchase_date, days_since_last_purchase, median_days_between_purchases, expected_next_purchase_date, days_overdue, unit_price, is_in_stock, shippable_qty, promo columns, customer_is_active, owner_rep_name, city |
| `ust_reporting.mart_social_trending_items` | matched_prtnum, is_rising, trend_rank, week_start, current_in_stock |

A **hand-made view** `ust_reporting.mart_account_item_pitch` currently sits
on top of `mart_account_item_opportunities`. It was built outside dbt as a
stopgap. Its SQL is `sql/mart_account_item_pitch.sql` in the voiceAI repo
(attached to this prompt). **Port it into dbt as a model of the same name and
columns**, since the assistant reads it today, then reuse its logic below.

---

## New seeds

### 1. `community_holidays` (exists; the sales team maintains it)
Attached as `community_holidays.csv`. Wide layout, one row per holiday:

```
holiday, community, date_varies, 2026, 2027, 2028, 2029, note
Hmong New Year, Hmong, yes, 12/1/2026, 12/1/2027, ...
Lunar New Year / Tết / Seollal, Chinese / Vietnamese / Korean, no, , 2/6/2027, ...
```

- Dates are `M/D/YYYY`. A blank cell means no date for that year.
- Year columns will be **added over time**, including **past years
  (2019–2025)** for the lift calculations below. Handle any column whose
  header is a four-digit year; never hard-code the list of years.
- `community` can list several, separated by `/`. Split them into one row
  each, trimmed and lower-cased.
- `American` and `All` holidays (Thanksgiving, July 4th) apply to **every**
  store.

Build `int_community_holidays`, unpivoted to grain
**(holiday, community, holiday_date)**, with `date_varies` as a boolean and
the original label kept as `communities`.

(The voiceAI repo currently loads the same CSV into
`ust_reporting.ref_community_holidays` with a script. Once your seed exists,
that table will be dropped, so name your model so it doesn't collide.)

### 2. `holiday_local_dates` (new, optional rows)
Some holidays are celebrated on different dates in different cities. Madison's
Hmong New Year in 2026 is Oct 31–Nov 1, while the calendar says Dec 1.
Columns: `holiday, city, state, start_date, end_date, note`. When a store's
city has a row, its local date replaces the calendar date for that store.
Create it with headers and that one example row.

### 3. `holiday_products` (new; voiceAI supplies the content)
What each holiday calls for, dish by dish. Generated once by the assistant's
model and reviewed by reps; not computed nightly. Columns:

```
holiday, community, dish, product, match_terms, why
Hmong New Year, hmong, Boiled pork & greens, Nam sausage, nam sausage|pork sausage, ...
```

`match_terms` is a `|`-separated list of phrases. An item matches a phrase
when every word of the phrase appears in `item_label` (case-insensitive).
Build `int_holiday_product_items`: **(holiday, community, dish, product,
item_no)** for every sellable, unblocked item that matches. Create the seed
with headers and a handful of example rows; the full file will be supplied.

### 4. `account_communities` (new; voiceAI supplies the content)
Which community each store serves. Columns: `customer_key, community, source,
confirmed`. Source is `rep` or `model`; confirmed is true/false. A store can
have more than one row. Create it with headers and example rows (HMO032 →
hmong, SHW007 → burmese, ISA001 → filipino). Stores with no row get only the
`American`/`All` holidays.

---

## The output: `mart_account_visit_plan`

**Grain:** one row per (customer_key, section, item_no), plus the occasion
columns when the section is `occasion`. Only active accounts
(`customer_is_active`). Include `owner_rep_name`, because the assistant
limits each rep to their own accounts.

**Sections** and what qualifies:

| section | What it is | How it qualifies |
|---|---|---|
| `occasion` | Items for a holiday in the **next 42 days** that this store's community celebrates (or American/All) | Item is in `int_holiday_product_items` for that holiday. Use the **local date** if the store's city has one. Carry holiday, holiday_date, days_until, dish, product, why |
| `season` | What this store buys more of in the coming 5 weeks, holiday or not | Store-level lift (below) ≥ 1.3 for the window from today to today + 35 days, by month-day, over 2019–2025 |
| `promo` | On promotion now, and the store buys it or its category | `is_on_promotion`, in stock, and the store bought the item in the last 365 days (or its `item_category_name` 3+ times) |
| `back_in_stock` | We were out recently and now have it | Store bought it in the last 365 days; `shippable_qty <= 0` on any day in the last 60 days of inventory history; in stock now |
| `due` | Regular items due again | `purchase_pattern = 'regular'` and days_overdue > 0 (from the pitch model) |
| `winback` | Lapsed items with a reason to raise them | `purchase_pattern = 'lapsed'` **and** at least one reason: out of stock when due, a backorder request, or a price change ≥ $0.50 since they last paid. Lapsed items with no reason are left out |
| `new_trending` | New items, or rising on social, in categories they buy | `is_new_item`, or in `mart_social_trending_items` with `is_rising` this week; in stock; the store bought that category 3+ times |
| `staple` | What they buy most | Top items by distinct purchase days in the last 365 days |

**Rules that matter to the sales team:**
- **A lapsed item is never an occasion item just because it's lapsed.** It
  appears under `occasion` only if it matches the holiday's products, and is
  ranked by the evidence below like any other.
- **"Bought during that window" is not holiday evidence.** A store that buys
  an item every few weeks will always have bought it in any 5-week window.
  Evidence is a lift: more than this store's own usual rate.
- **Out-of-stock items** may appear (the rep should know), but they get
  `can_lead = false` and are ranked last in their section.
- A store-level lift needs at least **3 purchases of the item in the
  2019–2025 windows**; a community lift needs at least **3 stores**.
  Otherwise the lift is null. Small samples showed spurious 5× results.

**Evidence columns** (null where they don't apply):

- `times_bought`, `first_bought`, `last_bought`, `days_bought_365`
- `last_paid_unit_price`, `price_unit`, `price_now`, `price_change_pct`.
  Compare only when the last purchase's `quantity_uom` matches the item's
  `base_uom`. Map IP→INNERPACK, CS→CASE, LB→LBS, as in the hand-made view.
- `store_lift`: this store's average quantity in the 35 days before the date,
  ÷ its average 35-day quantity, over 2019–2025.
  - For `occasion`, the date is the holiday's date **in each past year** from
    `int_community_holidays`, not a month-day, because lunar holidays move.
    If a holiday has no dates for past years, leave it null.
  - For `season`, the date is today's month-day in each past year.
- `community_lift`: the same lift across all stores in `account_communities`
  for that community (minimum 3 stores).
- `store_lift_years`: how many past years the lift is based on.
- `oos_days_recent`, `oos_from`, `oos_to`, `oos_days_when_due` (from the
  pitch model)
- `backorder_requests`, `last_backorder_date`
- `is_on_promotion`, `promo_price`, `promo_buy_qty`, `promo_free_qty`,
  `promo_end_date`
- `is_in_stock`, `shippable_qty`, `stock_snapshot_date`
- `evidence_strength`: `strong` / `some` / `weak`.
  - **strong:** in stock and (store_lift ≥ 1.3, or times_bought ≥ 4 with
    community_lift ≥ 1.15)
  - **some:** in stock and (times_bought > 0, or community_lift ≥ 1.15)
  - **weak:** otherwise
- `can_lead`: in stock and evidence_strength in (strong, some)
- `rank_in_section`: by can_lead, then strength, then lift, then recency.
  Keep at most **8 rows per section** per store, and at most **6 occasion
  items per dish**.
- `as_of_date`: the run date

The assistant chooses what to say from this table, so it must be complete,
not pre-trimmed to two or three items.

---

## Tests

- Standard: not_null and unique on the grain, accepted_values on `section`
  and `evidence_strength`, relationships to `dim_customers` and `dim_items`.
- Seed checks: every `community_holidays` date parses; every
  `holiday_products` row matches at least one item (warn, don't fail); every
  `account_communities.customer_key` exists.
- **Regression expectations** (warn level). Expected after the seeds above
  are filled in, using Madison's local date of Oct 31 for Hmong New Year, and
  as of late Oct 2026:
  - HMO032 (Hmong Legacy Market, Madison): `occasion` rows for Hmong New Year
    include Nam Sausage with store_lift ≈ 2.1 and Banh Pho noodles ≈ 1.5;
    `back_in_stock` or `winback` includes the Alum Cooking Pot (40cm); no
    lapsed item ranks first in `occasion` without a lift.
  - SHW007 (Shwe Market, Columbia): Thadingyut `occasion` rows include
    coconut milk (store_lift ≈ 1.6–1.9).
  - ISA001 (Isabela Asian Market, Billings): the `season` section includes
    salted duck eggs and Knorr beef bouillon.
  - VIE041 (Viet Hoa Market, Madison): no `occasion` rows for Diwali.

---

## Scheduling

Run nightly, after the models it depends on have refreshed (invoices, item
dimension, inventory snapshot). Use the project's existing orchestration.
The mart should be ready before 6 a.m. Central.

---

## Report back with

1. The models created, their materializations and dependencies.
2. The final column list of `mart_account_visit_plan` and the ported
   `mart_account_item_pitch`, with types.
3. Run time, row count, and rows per section.
4. Any source column that differed from this brief, and what you did about it.
5. Any rule you couldn't implement as written, and why.

Don't change existing models' columns, and don't drop anything. Removing the
hand-made view and `ref_community_holidays` will be done from the voiceAI side
once your models are confirmed.

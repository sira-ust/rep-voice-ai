-- mart_account_item_pitch: what to pitch one account, with the facts behind
-- each item, for the voice agent's lookup_pitch_list.
--
-- A hand-made VIEW over the dbt models, not a dbt model itself: it adds to
-- mart_account_item_opportunities and changes nothing in it. Port it into the
-- dbt project as a model (or fold the columns into the mart) and drop this.
--
--     python sql/apply.py            # (re)creates the view from this file
--
-- What it adds, per account and item:
--   purchase_pattern   regular  -- bought 4+ times and within twice its usual gap
--                      lapsed   -- bought before, but rarely or not for a long time
--                      suggestion -- never bought; similar stores or a promotion
--   last_paid_*        what the account paid per unit on its last purchase, only
--                      when that unit is the one unit_price is quoted in
--   oos_days_when_due  days we had none to ship, from two weeks before the item
--                      came due to 90 days after. Stock history starts
--                      2025-10-27, so stock_days_checked = 0 means "unknown",
--                      never "was in stock".
--   backorder_*        the account asked for it in the app while we were out
--                      (app events start Feb 2026)
--   suggested_order    regular items first, then lapsed ones with a reason to
--                      raise them (we ran out, they asked, the price moved),
--                      then the rest. A hint for the agent, not the answer.

CREATE OR REPLACE VIEW ust_databricks.ust_reporting.mart_account_item_pitch
COMMENT 'Hand-made view, not dbt: mart_account_item_opportunities plus last price paid, stock trouble when due, backorder requests and purchase pattern. Source: sql/mart_account_item_pitch.sql in voiceAI. Port into dbt.'
AS
WITH opp AS (
    SELECT * FROM ust_databricks.ust_reporting.mart_account_item_opportunities
),

last_buy AS (
    SELECT p.customer_key, p.item_no, p.posting_date AS last_paid_date,
           upper(p.quantity_uom) AS last_paid_uom,
           p.sold_value / p.sold_quantity AS last_paid_unit_price
    FROM ust_databricks.ust_intermediate.int_customer_item_purchases p
    JOIN opp o ON o.customer_key = p.customer_key AND o.item_no = p.item_no
    WHERE p.sold_quantity > 0 AND p.sold_value > 0
    QUALIFY row_number() OVER (PARTITION BY p.customer_key, p.item_no
                               ORDER BY p.posting_date DESC, p.sold_quantity DESC) = 1
),

stock_when_due AS (
    SELECT o.customer_key, o.item_no,
           count(s.snapshot_date) AS stock_days_checked,
           sum(CASE WHEN s.shippable_qty <= 0 THEN 1 ELSE 0 END) AS oos_days_when_due,
           min(CASE WHEN s.shippable_qty <= 0 THEN s.snapshot_date END) AS first_oos_when_due,
           max(CASE WHEN s.shippable_qty <= 0 THEN s.snapshot_date END) AS last_oos_when_due
    FROM opp o
    JOIN ust_databricks.ust_intermediate.int_jdawms_inventory_daily s
      ON s.prtnum = o.item_no
     AND s.snapshot_date BETWEEN date_sub(o.expected_next_purchase_date, 14)
                             AND date_add(o.expected_next_purchase_date, 90)
    WHERE o.expected_next_purchase_date IS NOT NULL
    GROUP BY o.customer_key, o.item_no
),

-- The app logs "item_no, qty" when an item goes on the backorder page.
backorders AS (
    SELECT e.customer_key, trim(split(e.response, ',')[0]) AS item_no,
           count(*) AS backorder_requests,
           max(e.rep_local_date) AS last_backorder_date,
           max(try_cast(trim(split(e.response, ',')[1]) AS DOUBLE)) AS backorder_qty
    FROM ust_databricks.ust_intermediate.int_events_enriched e
    WHERE e.function_name = 'add_item_backorder_page'
      AND e.customer_key IS NOT NULL AND e.response LIKE '%,%'
    GROUP BY e.customer_key, trim(split(e.response, ',')[0])
),

facts AS (
    SELECT o.*,
           CASE WHEN NOT o.is_evidence_based THEN 'suggestion'
                WHEN o.times_purchased >= 4
                 AND o.days_since_last_purchase <= 2 * o.median_days_between_purchases THEN 'regular'
                ELSE 'lapsed' END AS purchase_pattern,
           l.last_paid_date,
           -- Only comparable in the unit the list price is quoted in.
           CASE WHEN l.last_paid_uom = CASE upper(d.base_uom) WHEN 'IP' THEN 'INNERPACK'
                                                              WHEN 'CS' THEN 'CASE'
                                                              WHEN 'LB' THEN 'LBS'
                                                              ELSE upper(d.base_uom) END
                THEN round(l.last_paid_unit_price, 2) END AS last_paid_unit_price,
           lower(d.base_uom) AS price_unit,
           coalesce(s.stock_days_checked, 0) AS stock_days_checked,
           coalesce(s.oos_days_when_due, 0) AS oos_days_when_due,
           s.first_oos_when_due, s.last_oos_when_due,
           coalesce(b.backorder_requests, 0) AS backorder_requests,
           b.last_backorder_date, b.backorder_qty
    FROM opp o
    LEFT JOIN ust_databricks.ust_dims.dim_items d ON d.item_no = o.item_no
    LEFT JOIN last_buy l ON l.customer_key = o.customer_key AND l.item_no = o.item_no
    LEFT JOIN stock_when_due s ON s.customer_key = o.customer_key AND s.item_no = o.item_no
    LEFT JOIN backorders b ON b.customer_key = o.customer_key AND b.item_no = o.item_no
)

SELECT f.*,
       CASE WHEN f.last_paid_unit_price > 0
            THEN round(100 * (f.unit_price / f.last_paid_unit_price - 1)) END AS price_change_pct,
       row_number() OVER (
           PARTITION BY f.customer_key
           ORDER BY CASE WHEN f.purchase_pattern = 'regular' THEN 0
                         WHEN f.purchase_pattern = 'lapsed'
                          AND (f.oos_days_when_due > 0 OR f.backorder_requests > 0
                               OR abs(f.unit_price - f.last_paid_unit_price) >= 0.5) THEN 1
                         WHEN f.purchase_pattern = 'lapsed' THEN 2
                         ELSE 3 END,
                    f.opportunity_rank) AS suggested_order
FROM facts f

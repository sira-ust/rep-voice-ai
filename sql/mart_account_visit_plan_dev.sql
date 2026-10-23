-- mart_account_visit_plan_dev: a stand-in for the dbt model mart_account_visit_plan
-- (prompts/dbt_visit_plan.md), so the voice agent can be built and tested before
-- that model exists. Same grain and column names, fewer refinements:
--
--   * the `occasion` section reads the ref_* tables that sql/load_holidays.py
--     loads (calendar, holiday products, store communities, local dates).
--   * a holiday's past-year window is by this year's month and day, not by each
--     past year's own date: close for fixed holidays, approximate for lunar ones
--     until the calendar has 2019-2025 dates.
--   * the season window is by day of year.
--
-- A TABLE, rebuilt on demand -- the nightly run stands in for itself:
--
--     python sql/apply.py sql/mart_account_visit_plan_dev.sql
--
-- When the dbt model is live, point VISIT_PLAN_TABLE at it and drop this table.

CREATE OR REPLACE TABLE ust_databricks.ust_reporting.mart_account_visit_plan_dev
COMMENT 'Hand-made stand-in for the dbt model mart_account_visit_plan. Source: sql/mart_account_visit_plan_dev.sql in voiceAI. Drop once the dbt model is live.'
AS
WITH accounts AS (
    SELECT DISTINCT customer_key, owner_rep_name
    FROM ust_databricks.ust_reporting.mart_account_item_opportunities
    WHERE customer_is_active AND owner_rep_name IS NOT NULL
),

items AS (
    SELECT item_no, item_label, item_category_name, unit_price, base_uom, case_qty, is_in_stock, shippable_qty,
           stock_snapshot_date, is_on_promotion, promo_price, promo_buy_qty, promo_free_qty,
           promo_end_date, is_new_item
    FROM ust_databricks.ust_dims.dim_items
    WHERE is_sellable AND NOT is_blocked
),

buys AS (
    SELECT p.customer_key, p.item_no, p.posting_date, p.sold_quantity, p.sold_value,
           upper(p.quantity_uom) AS uom
    FROM ust_databricks.ust_intermediate.int_customer_item_purchases p
    JOIN accounts a USING (customer_key)
    WHERE p.sold_quantity > 0
),

-- Per account and item. The season window is the 36 days from today's day of
-- the year, in 2019-2025, against a usual 36 days in those years.
history AS (
    SELECT customer_key, item_no,
           count(DISTINCT posting_date) AS times_bought,
           min(posting_date) AS first_bought,
           max(posting_date) AS last_bought,
           count(DISTINCT CASE WHEN posting_date >= date_sub(current_date(), 365) THEN posting_date END)
               AS days_bought_365,
           max_by(sold_value / sold_quantity, posting_date) AS last_paid_raw,
           max_by(uom, posting_date) AS last_paid_uom,
           sum(CASE WHEN year(posting_date) BETWEEN 2019 AND 2025
                     AND pmod(dayofyear(posting_date) - dayofyear(current_date()), 366) < 36
                    THEN sold_quantity ELSE 0 END) / 7 AS season_qty,
           count(DISTINCT CASE WHEN year(posting_date) BETWEEN 2019 AND 2025
                     AND pmod(dayofyear(posting_date) - dayofyear(current_date()), 366) < 36
                    THEN posting_date END) AS season_buys,
           sum(CASE WHEN year(posting_date) BETWEEN 2019 AND 2025 THEN sold_quantity ELSE 0 END)
               / (7 * 366 / 36.0) AS usual_qty,
           -- the last 6 months against the 6 before, for "buying less lately"
           sum(CASE WHEN posting_date >= date_sub(current_date(), 180) THEN sold_quantity ELSE 0 END)
               AS recent_qty,
           sum(CASE WHEN posting_date < date_sub(current_date(), 180)
                     AND posting_date >= date_sub(current_date(), 360) THEN sold_quantity ELSE 0 END)
               AS prior_qty,
           count(DISTINCT CASE WHEN posting_date < date_sub(current_date(), 180)
                     AND posting_date >= date_sub(current_date(), 360) THEN posting_date END)
               AS prior_days
    FROM buys
    GROUP BY customer_key, item_no
),

categories AS (
    SELECT b.customer_key, i.item_category_name, count(DISTINCT b.posting_date) AS category_days
    FROM buys b JOIN items i USING (item_no)
    WHERE b.posting_date >= date_sub(current_date(), 365)
    GROUP BY b.customer_key, i.item_category_name
),

recent_oos AS (
    SELECT prtnum AS item_no, count(*) AS oos_days_recent,
           min(snapshot_date) AS oos_from, max(snapshot_date) AS oos_to
    FROM ust_databricks.ust_intermediate.int_jdawms_inventory_daily
    WHERE shippable_qty <= 0 AND snapshot_date >= date_sub(current_date(), 60)
    GROUP BY prtnum
),

trending AS (
    SELECT DISTINCT matched_prtnum AS item_no
    FROM ust_databricks.ust_reporting.mart_social_trending_items
    WHERE is_rising AND matched_prtnum IS NOT NULL
      AND week_start = (SELECT max(week_start) FROM ust_databricks.ust_reporting.mart_social_trending_items)
),

pitch AS (
    SELECT * FROM ust_databricks.ust_reporting.mart_account_item_pitch
),

-- ---------------------------------------------------------------- occasion
-- The communities a store serves, in the calendar's words, plus American and
-- All, which every store keeps.
community_map AS (
    SELECT * FROM VALUES
        ('indian', 'hindu'), ('mexican', 'mexican american'), ('mexican', 'hispanic'),
        ('hispanic', 'mexican'), ('malay', 'malaysian'), ('filipino', 'christian'),
        ('hispanic', 'christian'), ('sikh', 'punjabi')
        AS t(store_community, calendar_community)
),

store_tokens AS (
    SELECT customer_key, community AS token FROM ust_databricks.ust_reporting.ref_account_communities
    UNION
    SELECT c.customer_key, m.calendar_community
    FROM ust_databricks.ust_reporting.ref_account_communities c
    JOIN community_map m ON m.store_community = c.community
    UNION
    SELECT customer_key, 'american' FROM accounts
    UNION
    SELECT customer_key, 'all' FROM accounts
),

-- Holidays in the next 42 days for each store, on its city's own date where
-- there is one. is_general: only American/All matched, so the holiday has to
-- show a lift in the data to be raised at all.
upcoming AS (
    SELECT st.customer_key, h.holiday,
           coalesce(ld.start_date, h.holiday_date) AS holiday_date,
           bool_and(h.community IN ('american', 'all')) AS is_general
    FROM store_tokens st
    JOIN ust_databricks.ust_reporting.ref_community_holidays h ON h.community = st.token
    JOIN accounts a ON a.customer_key = st.customer_key
    LEFT JOIN ust_databricks.ust_dims.dim_customers c ON c.customer_key = st.customer_key
    LEFT JOIN ust_databricks.ust_reporting.ref_holiday_local_dates ld
      ON ld.holiday = h.holiday AND lower(ld.city) = lower(c.city)
     AND year(ld.start_date) = year(h.holiday_date)
    WHERE coalesce(ld.start_date, h.holiday_date) BETWEEN current_date() AND date_add(current_date(), 42)
    GROUP BY st.customer_key, h.holiday, coalesce(ld.start_date, h.holiday_date)
),

holiday_terms AS (
    SELECT holiday, dish, product, why, trim(term) AS term
    FROM (SELECT *, explode(split(match_terms, '[|]')) AS term
          FROM ust_databricks.ust_reporting.ref_holiday_products)
    WHERE trim(term) <> ''
),

-- An item matches a term when every word of the term is in its label.
product_items AS (
    SELECT DISTINCT t.holiday, t.dish, t.product, t.why, i.item_no
    FROM holiday_terms t
    JOIN items i
      ON forall(filter(split(lower(t.term), ' '), w -> length(w) > 1),
                w -> instr(lower(i.item_label), w) > 0)
),

occasion_pairs AS (
    SELECT u.customer_key, u.holiday, u.holiday_date, u.is_general,
           p.dish, p.product, p.why, p.item_no
    FROM upcoming u JOIN product_items p USING (holiday)
),

-- This store, the five weeks before the holiday's month and day, 2019-2025.
occasion_store AS (
    SELECT o.customer_key, o.item_no, o.holiday,
           sum(CASE WHEN year(b.posting_date) BETWEEN 2019 AND 2025
                     AND datediff(make_date(year(b.posting_date), month(o.holiday_date), day(o.holiday_date)),
                                  b.posting_date) BETWEEN 0 AND 34
                    THEN b.sold_quantity ELSE 0 END) / 7 AS before_qty,
           count(DISTINCT CASE WHEN year(b.posting_date) BETWEEN 2019 AND 2025
                     AND datediff(make_date(year(b.posting_date), month(o.holiday_date), day(o.holiday_date)),
                                  b.posting_date) BETWEEN 0 AND 34
                    THEN b.posting_date END) AS before_buys,
           sum(CASE WHEN year(b.posting_date) BETWEEN 2019 AND 2025 THEN b.sold_quantity ELSE 0 END)
               / (7 * 365 / 35.0) AS usual_qty
    FROM (SELECT DISTINCT customer_key, item_no, holiday, holiday_date FROM occasion_pairs) o
    JOIN buys b ON b.customer_key = o.customer_key AND b.item_no = o.item_no
    GROUP BY o.customer_key, o.item_no, o.holiday
),

-- Stores that celebrate the holiday (every store, for American/All ones).
holiday_peers AS (
    SELECT DISTINCT h.holiday, st.customer_key
    FROM ust_databricks.ust_reporting.ref_community_holidays h
    JOIN store_tokens st ON st.token = h.community
    WHERE h.community NOT IN ('american', 'all')
    UNION
    SELECT DISTINCT h.holiday, a.customer_key
    FROM ust_databricks.ust_reporting.ref_community_holidays h CROSS JOIN accounts a
    WHERE h.community IN ('american', 'all')
),

-- Those stores together, the four weeks before, 2023-2025.
occasion_peers AS (
    SELECT x.holiday, x.item_no,
           sum(CASE WHEN datediff(make_date(year(b.posting_date), month(x.holiday_date), day(x.holiday_date)),
                                  b.posting_date) BETWEEN 0 AND 27
                    THEN b.sold_quantity ELSE 0 END) / 3 AS before_qty,
           sum(b.sold_quantity) / 39.0 AS usual_qty,
           count(DISTINCT b.customer_key) AS stores
    FROM (SELECT DISTINCT holiday, holiday_date, item_no FROM occasion_pairs) x
    JOIN holiday_peers hp ON hp.holiday = x.holiday
    JOIN buys b ON b.customer_key = hp.customer_key AND b.item_no = x.item_no
    WHERE b.posting_date >= '2023-01-01' AND b.posting_date < '2026-01-01'
    GROUP BY x.holiday, x.item_no
),

occasion AS (
    SELECT p.*,
           CASE WHEN s.before_buys >= 3 AND s.usual_qty > 0 THEN round(s.before_qty / s.usual_qty, 2) END
               AS occasion_store_lift,
           CASE WHEN pr.stores >= 3 AND pr.usual_qty > 0 THEN round(pr.before_qty / pr.usual_qty, 2) END
               AS peer_lift
    FROM occasion_pairs p
    LEFT JOIN occasion_store s ON s.customer_key = p.customer_key AND s.item_no = p.item_no
                              AND s.holiday = p.holiday
    LEFT JOIN occasion_peers pr ON pr.holiday = p.holiday AND pr.item_no = p.item_no
),

-- ---------------------------------------------------------------- more idea types

-- Our stock against its own level over the last 90 days -- same units on both
-- sides, so no case/each confusion. 0.25 = a quarter of what we usually hold.
stock_level AS (
    SELECT prtnum AS item_no, avg(shippable_qty) AS avg_shippable_90
    FROM ust_databricks.ust_intermediate.int_jdawms_inventory_daily
    WHERE snapshot_date >= date_sub(current_date(), 90)
    GROUP BY prtnum
),

-- The meaningful words of a label: lower case, letters and digits, 3+ long.
label_words AS (
    SELECT item_no, item_label, item_category_name, unit_price, case_qty, is_in_stock,
           upper(item_label) LIKE 'DF %' OR upper(item_label) LIKE 'DRAGONFLY%'
               OR upper(item_label) LIKE 'FZ DF %' AS is_own_brand,
           filter(split(lower(regexp_replace(item_label, '[^A-Za-z0-9 ]', ' ')), ' '),
                  w -> length(w) > 2 AND w NOT IN ('dragonfly', 'pcs', 'pkt', 'pkts', 'can', 'bottle')) AS words
    FROM items
),

-- Items they buy that we have none of now, and the closest item we do have:
-- same category, at least two words of the name in common.
alternatives AS (
    SELECT h.customer_key, a.item_no, o.item_label AS alt_for, o.unit_price AS alt_for_price,
           o.case_qty AS alt_for_case_qty
    FROM history h
    JOIN label_words o ON o.item_no = h.item_no AND NOT o.is_in_stock
    JOIN label_words a ON a.item_category_name = o.item_category_name AND a.is_in_stock
                      AND a.item_no <> o.item_no
    WHERE h.days_bought_365 > 0 AND size(array_intersect(o.words, a.words)) >= 2
    QUALIFY row_number() OVER (PARTITION BY h.customer_key, o.item_no
                               ORDER BY size(array_intersect(o.words, a.words)) DESC, a.unit_price) = 1
),

-- Our own brand for something they buy from another brand: same category, two
-- words in common, one the store does not already buy.
own_brand AS (
    SELECT h.customer_key, d.item_no, o.item_label AS alt_for, o.unit_price AS alt_for_price,
           o.case_qty AS alt_for_case_qty
    FROM history h
    JOIN label_words o ON o.item_no = h.item_no AND NOT o.is_own_brand
    JOIN label_words d ON d.item_category_name = o.item_category_name AND d.is_own_brand AND d.is_in_stock
    LEFT JOIN history mine ON mine.customer_key = h.customer_key AND mine.item_no = d.item_no
    WHERE h.days_bought_365 >= 2 AND mine.item_no IS NULL
      AND size(array_intersect(o.words, d.words)) >= 2
    QUALIFY row_number() OVER (PARTITION BY h.customer_key, o.item_no
                               ORDER BY size(array_intersect(o.words, d.words)) DESC, d.unit_price) = 1
),

-- What stores in the same community commonly buy (30% of them in the last
-- year) and this store never has.
community_size AS (
    SELECT community, count(DISTINCT customer_key) AS stores
    FROM ust_databricks.ust_reporting.ref_account_communities GROUP BY community
),
community_items AS (
    SELECT c.community, b.item_no, count(DISTINCT b.customer_key) AS buyers
    FROM ust_databricks.ust_reporting.ref_account_communities c
    JOIN buys b ON b.customer_key = c.customer_key
    WHERE b.posting_date >= date_sub(current_date(), 365)
    GROUP BY c.community, b.item_no
),
peer_gap AS (
    SELECT ac.customer_key, ci.item_no, max(round(ci.buyers / cs.stores, 2)) AS peer_share
    FROM ust_databricks.ust_reporting.ref_account_communities ac
    JOIN community_items ci USING (community)
    JOIN community_size cs USING (community)
    JOIN items i ON i.item_no = ci.item_no
    LEFT JOIN history h ON h.customer_key = ac.customer_key AND h.item_no = ci.item_no
    WHERE h.item_no IS NULL AND i.is_in_stock AND cs.stores >= 5 AND ci.buyers >= 3
      AND ci.buyers / cs.stores >= 0.3
    GROUP BY ac.customer_key, ci.item_no
),

-- What every store has paid per unit lately against 4-8 months ago. A step of
-- a third or more for everyone at once is more likely a new pack size than a
-- price change: Teans crispy chili went from ~$125 to ~$68 a case for all its
-- stores in August, and "47% cheaper than they last paid" was not true.
price_step AS (
    SELECT item_no,
           try_divide(sum(CASE WHEN posting_date >= date_sub(current_date(), 60) THEN sold_value END),
                      sum(CASE WHEN posting_date >= date_sub(current_date(), 60) THEN sold_quantity END))
               AS recent_price,
           try_divide(sum(CASE WHEN posting_date BETWEEN date_sub(current_date(), 240)
                                                 AND date_sub(current_date(), 120) THEN sold_value END),
                      sum(CASE WHEN posting_date BETWEEN date_sub(current_date(), 240)
                                                 AND date_sub(current_date(), 120) THEN sold_quantity END))
               AS earlier_price
    FROM buys
    GROUP BY item_no
),

candidates AS (
    SELECT DISTINCT customer_key, item_no, 'occasion' AS section,
           CAST(NULL AS STRING) AS alt_for, CAST(NULL AS DECIMAL(18, 6)) AS alt_for_price,
           CAST(NULL AS DOUBLE) AS peer_share, CAST(NULL AS DECIMAL(18, 6)) AS alt_for_case_qty
    FROM occasion
    UNION ALL
    SELECT customer_key, item_no, 'staple', NULL, NULL, NULL, NULL FROM history WHERE days_bought_365 > 0
    UNION ALL
    SELECT customer_key, item_no, 'season', NULL, NULL, NULL, NULL FROM history
    WHERE season_buys >= 3 AND usual_qty > 0 AND season_qty / usual_qty >= 1.3
    UNION ALL
    SELECT h.customer_key, h.item_no, 'promo', NULL, NULL, NULL, NULL FROM history h JOIN items i USING (item_no)
    WHERE i.is_on_promotion AND i.is_in_stock AND h.days_bought_365 > 0
    UNION ALL
    SELECT c.customer_key, i.item_no, 'promo', NULL, NULL, NULL, NULL
    FROM categories c JOIN items i USING (item_category_name)
    LEFT JOIN history h ON h.customer_key = c.customer_key AND h.item_no = i.item_no
    WHERE i.is_on_promotion AND i.is_in_stock AND c.category_days >= 3 AND h.item_no IS NULL
    UNION ALL
    -- promotions on what they buy that end within a week
    SELECT h.customer_key, h.item_no, 'promo_ending', NULL, NULL, NULL, NULL FROM history h JOIN items i USING (item_no)
    WHERE i.is_on_promotion AND i.is_in_stock AND h.days_bought_365 > 0
      AND i.promo_end_date BETWEEN current_date() AND date_add(current_date(), 7)
    UNION ALL
    SELECT h.customer_key, h.item_no, 'back_in_stock', NULL, NULL, NULL, NULL FROM history h
    JOIN recent_oos o USING (item_no) JOIN items i USING (item_no)
    WHERE h.days_bought_365 > 0 AND i.is_in_stock
    UNION ALL
    -- now cheaper than they last paid (5% or more; checked in the same unit below)
    SELECT h.customer_key, h.item_no, 'price_drop', NULL, NULL, NULL, NULL FROM history h JOIN items i USING (item_no)
    WHERE h.days_bought_365 > 0 AND i.is_in_stock AND h.last_paid_raw > 0
      AND i.unit_price <= h.last_paid_raw * 0.95
    UNION ALL
    -- they buy it, and our stock is down to a quarter of its usual level
    SELECT h.customer_key, h.item_no, 'low_stock', NULL, NULL, NULL, NULL FROM history h
    JOIN items i USING (item_no) JOIN stock_level s USING (item_no)
    WHERE h.days_bought_365 >= 2 AND i.is_in_stock AND s.avg_shippable_90 > 0
      AND i.shippable_qty <= 0.25 * s.avg_shippable_90
    UNION ALL
    -- the last 6 months at 40% or less of the 6 before
    SELECT customer_key, item_no, 'buying_less', NULL, NULL, NULL, NULL FROM history
    WHERE prior_days >= 3 AND recent_qty <= 0.4 * prior_qty
    UNION ALL
    SELECT customer_key, item_no, 'alternative', alt_for, alt_for_price, NULL, alt_for_case_qty FROM alternatives
    UNION ALL
    SELECT customer_key, item_no, 'own_brand', alt_for, alt_for_price, NULL, alt_for_case_qty FROM own_brand
    UNION ALL
    SELECT customer_key, item_no, 'peer_gap', NULL, NULL, peer_share, NULL FROM peer_gap
    UNION ALL
    SELECT c.customer_key, i.item_no, 'new_trending', NULL, NULL, NULL, NULL
    FROM categories c JOIN items i USING (item_category_name)
    LEFT JOIN trending t USING (item_no)
    LEFT JOIN history h ON h.customer_key = c.customer_key AND h.item_no = i.item_no
    WHERE (i.is_new_item OR t.item_no IS NOT NULL) AND i.is_in_stock AND c.category_days >= 3
      AND h.item_no IS NULL
    UNION ALL
    SELECT customer_key, item_no, 'due', NULL, NULL, NULL, NULL FROM pitch
    WHERE purchase_pattern = 'regular' AND days_overdue > 0
    UNION ALL
    SELECT customer_key, item_no, 'winback', NULL, NULL, NULL, NULL FROM pitch
    WHERE purchase_pattern = 'lapsed'
      AND (oos_days_when_due > 0 OR backorder_requests > 0
           OR abs(unit_price - last_paid_unit_price) >= 0.5)
),

facts AS (
    SELECT DISTINCT c.customer_key, a.owner_rep_name, c.section, c.item_no,
           i.item_label, i.item_category_name,
           h.times_bought, h.first_bought, h.last_bought, h.days_bought_365,
           CASE WHEN h.last_paid_uom = CASE upper(i.base_uom) WHEN 'IP' THEN 'INNERPACK'
                                                          WHEN 'CS' THEN 'CASE'
                                                          WHEN 'LB' THEN 'LBS'
                                                          ELSE upper(i.base_uom) END
                THEN round(h.last_paid_raw, 2) END AS last_paid_unit_price,
           lower(i.base_uom) AS price_unit,
           i.unit_price AS price_now,
           CASE WHEN h.season_buys >= 3 AND h.usual_qty > 0 THEN round(h.season_qty / h.usual_qty, 2) END
               AS store_lift,
           cat.category_days,
           o.oos_days_recent, o.oos_from, o.oos_to,
           p.purchase_pattern, p.median_days_between_purchases, p.days_overdue,
           p.oos_days_when_due, p.first_oos_when_due, p.last_oos_when_due,
           p.backorder_requests, p.last_backorder_date, p.suggested_order,
           i.is_on_promotion, i.promo_price, i.promo_buy_qty, i.promo_free_qty, i.promo_end_date,
           i.is_new_item, t.item_no IS NOT NULL AS is_trending,
           i.is_in_stock, i.shippable_qty, i.stock_snapshot_date,
           c.alt_for, c.alt_for_price, c.peer_share, c.alt_for_case_qty, i.case_qty,
           coalesce(try_divide(ps.recent_price, ps.earlier_price) NOT BETWEEN 0.65 AND 1.5, false)
               AS pack_change_suspect,
           CASE WHEN h.prior_qty > 0 THEN round(h.recent_qty / h.prior_qty, 2) END AS recent_vs_prior,
           CASE WHEN sl.avg_shippable_90 > 0 THEN round(i.shippable_qty / sl.avg_shippable_90, 2) END
               AS stock_vs_usual,
           datediff(i.promo_end_date, current_date()) AS promo_days_left
    FROM candidates c
    JOIN accounts a USING (customer_key)
    JOIN items i USING (item_no)
    LEFT JOIN stock_level sl ON sl.item_no = c.item_no
    LEFT JOIN price_step ps ON ps.item_no = c.item_no
    LEFT JOIN history h ON h.customer_key = c.customer_key AND h.item_no = c.item_no
    LEFT JOIN categories cat ON cat.customer_key = c.customer_key AND cat.item_category_name = i.item_category_name
    LEFT JOIN recent_oos o ON o.item_no = c.item_no
    LEFT JOIN trending t ON t.item_no = c.item_no
    LEFT JOIN pitch p ON p.customer_key = c.customer_key AND p.item_no = c.item_no
),

-- Occasion rows carry their holiday and dish, and their own lift (before the
-- holiday, not the next five weeks).
joined AS (
    SELECT f.*, o.holiday, o.holiday_date, datediff(o.holiday_date, current_date()) AS days_until,
           o.dish, o.product, o.why AS holiday_why, o.is_general, o.peer_lift, o.occasion_store_lift
    FROM facts f
    LEFT JOIN occasion o ON f.section = 'occasion' AND o.customer_key = f.customer_key
                        AND o.item_no = f.item_no
),

scored AS (
    SELECT j.* EXCEPT (store_lift, occasion_store_lift),
           CASE WHEN j.section = 'occasion' THEN j.occasion_store_lift ELSE j.store_lift END AS store_lift,
           CASE WHEN j.last_paid_unit_price > 0
                THEN round(100 * (j.price_now / j.last_paid_unit_price - 1)) END AS price_change_pct,
           CASE WHEN NOT j.is_in_stock THEN 'weak'
                WHEN j.section = 'occasion' THEN
                     CASE WHEN coalesce(j.occasion_store_lift, 0) >= 1.3
                            OR (coalesce(j.times_bought, 0) >= 4 AND coalesce(j.peer_lift, 0) >= 1.15) THEN 'strong'
                          WHEN coalesce(j.times_bought, 0) > 0 OR coalesce(j.peer_lift, 0) >= 1.15 THEN 'some'
                          ELSE 'weak' END
                WHEN coalesce(j.store_lift, 0) >= 1.3 THEN 'strong'
                WHEN coalesce(j.days_bought_365, 0) > 0 OR j.section IN ('due', 'winback') THEN 'some'
                ELSE 'weak' END AS evidence_strength,
           coalesce(j.is_in_stock, false) AS can_lead
    FROM joined j
),

-- An occasion item needs a lift before the holiday -- this store's, or that of
-- the stores that celebrate it. Buying it at all is not enough: "Pho spice
-- mix" came through for Hmong New Year at 1.0x, on a store that buys it all
-- year.
kept AS (
    SELECT * FROM scored
    WHERE CASE section
              -- American/All holidays (Thanksgiving) need this store's own lift:
              -- the all-store figure put condensed milk on nearly every card,
              -- for stores that have never bought it.
              WHEN 'occasion' THEN evidence_strength IN ('strong', 'some')
                               AND (coalesce(store_lift, 0) >= 1.3
                                    OR (NOT is_general AND coalesce(peer_lift, 0) >= 1.15))
              -- a drop only counts in the unit they last paid in
              WHEN 'price_drop' THEN coalesce(price_change_pct, 0) <= -5 AND NOT pack_change_suspect
              ELSE true END
    -- one row per item and dish, however many of the dish's products it matched
    QUALIFY row_number() OVER (PARTITION BY customer_key, section, item_no, coalesce(holiday, ''),
                                            coalesce(dish, '') ORDER BY product, alt_for) = 1
),

ranked AS (
    SELECT k.*,
           row_number() OVER (
               PARTITION BY k.customer_key, k.section, coalesce(k.holiday, ''), coalesce(k.dish, '')
               ORDER BY k.can_lead DESC,
                        CASE WHEN k.section = 'occasion' AND k.evidence_strength = 'strong' THEN 0 ELSE 1 END,
                        CASE k.section
                            WHEN 'occasion' THEN -(coalesce(k.store_lift, 0) + coalesce(k.peer_lift, 0))
                            WHEN 'season' THEN -k.store_lift
                            WHEN 'price_drop' THEN k.price_change_pct
                            WHEN 'promo_ending' THEN k.promo_days_left
                            WHEN 'low_stock' THEN k.stock_vs_usual
                            WHEN 'buying_less' THEN k.recent_vs_prior
                            WHEN 'peer_gap' THEN -k.peer_share
                            WHEN 'own_brand' THEN k.price_now - coalesce(k.alt_for_price, k.price_now)
                            WHEN 'due' THEN k.suggested_order
                            WHEN 'winback' THEN k.suggested_order
                            WHEN 'new_trending' THEN -k.category_days
                            ELSE -coalesce(k.days_bought_365, 0) END,
                        k.last_bought DESC NULLS LAST, k.item_no) AS rank_in_section
    FROM kept k
)

SELECT * EXCEPT (is_general), current_date() AS as_of_date
FROM ranked
WHERE rank_in_section <= CASE WHEN section = 'occasion' THEN 3 ELSE 8 END

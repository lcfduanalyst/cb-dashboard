-- MySQL 8.0.x
-- 条件价格指数：全量重算（首次灌历史 / 剔除规则变更后）
-- 使用前请先执行 schema_cb_conditional_price_index_mysql8.sql

TRUNCATE TABLE cb_daily_mean_price_cond;

INSERT INTO cb_daily_mean_price_cond (trade_date, mean_value)
WITH base AS (
  SELECT
    p.trade_date,
    p.bond_code,
    p.value AS price,
    c.value AS conv_premium,
    s.value AS scale_yi,
    UPPER(TRIM(REPLACE(r.value_text, '＋', '+'))) AS rating_norm
  FROM cb_panel_price p
  LEFT JOIN cb_panel_conv_premium_rate c
    ON c.trade_date = p.trade_date AND c.bond_code = p.bond_code
  LEFT JOIN cb_panel_scale s
    ON s.trade_date = p.trade_date AND s.bond_code = p.bond_code
  LEFT JOIN cb_panel_rating r
    ON r.trade_date = p.trade_date AND r.bond_code = p.bond_code
  WHERE p.value IS NOT NULL
    AND p.bond_code NOT IN ('123095.SZ')
),
eligible AS (
  SELECT trade_date, bond_code, price
  FROM base
  WHERE NOT (
       (price > 130 AND conv_premium > 50)
    OR (price > 120 AND conv_premium > 75)
    OR (price > 150)
    OR (scale_yi < 3)
    OR rating_norm IS NULL
    OR rating_norm NOT IN ('AAA', 'AA+', 'AA', 'AA-', 'A+')
  )
)
SELECT
  trade_date,
  AVG(price) AS mean_value
FROM eligible
GROUP BY trade_date;

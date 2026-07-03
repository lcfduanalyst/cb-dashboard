-- MySQL 8.0.x
-- 条件价格指数（筛选后价格样本均值）
--
-- 剔除规则（满足任一条即不参与）：
--   (1) 价格 > 130 且 转股溢价率 > 50%
--   (2) 价格 > 120 且 转股溢价率 > 75%
--   (3) 价格 > 150
--   (4) 规模 < 3 亿元（cb_panel_scale.value 单位为亿元）
--   (5) 无评级，或评级低于 A+（保留 AAA / AA+ / AA / AA- / A+）
--
-- 使用前请先执行：schema_cb_conditional_price_index_mysql8.sql
--
-- ========== A) 每日更新（Wind 同步后执行，重算最近 30 个交易日）==========
SET @recent_days := 30;

INSERT INTO cb_daily_mean_price_cond (trade_date, mean_value)
WITH last_days AS (
  SELECT DISTINCT trade_date
  FROM cb_panel_price
  ORDER BY trade_date DESC
  LIMIT @recent_days
),
base AS (
  SELECT
    p.trade_date,
    p.bond_code,
    p.value AS price,
    c.value AS conv_premium,
    s.value AS scale_yi,
    UPPER(TRIM(REPLACE(r.value_text, '＋', '+'))) AS rating_norm
  FROM cb_panel_price p
  JOIN last_days d ON d.trade_date = p.trade_date
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
GROUP BY trade_date
ON DUPLICATE KEY UPDATE
  mean_value = VALUES(mean_value);

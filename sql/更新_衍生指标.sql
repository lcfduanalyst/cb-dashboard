SET @N :=20;

INSERT INTO  cb_panel_conv_value (trade_date, bond_code, value)
SELECT
  p.trade_date,
  p.bond_code,
  CASE
    WHEN c.value IS NULL THEN NULL
    WHEN 1 + c.value / 100 = 0 THEN NULL
    ELSE p.value / (1 + c.value / 100)
  END AS conv_value
FROM cb_panel_price p
JOIN cb_panel_conv_premium_rate c
  ON c.trade_date = p.trade_date AND c.bond_code = p.bond_code
JOIN (
  SELECT DISTINCT trade_date
  FROM cb_panel_price
  ORDER BY trade_date DESC
  LIMIT 20
) d
  ON d.trade_date = p.trade_date
ON DUPLICATE KEY UPDATE value = VALUES(value);





--计算价格中位数
SET @N := 60;

INSERT INTO cb_daily_median_price (trade_date, median_value)
WITH last_days AS (
  SELECT DISTINCT trade_date
  FROM cb_panel_price
  ORDER BY trade_date DESC
  LIMIT 60
),
ranked AS (
  SELECT
    p.trade_date,
    p.value,
    ROW_NUMBER() OVER (PARTITION BY p.trade_date ORDER BY p.value) AS rn,
    COUNT(*)    OVER (PARTITION BY p.trade_date) AS cnt
  FROM cb_panel_price p
  JOIN last_days d ON d.trade_date = p.trade_date
  WHERE p.value IS NOT NULL
),
picked AS (
  SELECT trade_date, value
  FROM ranked
  WHERE rn IN (FLOOR((cnt + 1)/2), FLOOR((cnt + 2)/2))
)
SELECT
  trade_date,
  AVG(value) AS median_value
FROM picked
GROUP BY trade_date
ON DUPLICATE KEY UPDATE median_value = VALUES(median_value);





--计算YTM中位数
SET @N := 60;

INSERT INTO cb_daily_median_ytm (trade_date, median_value)
WITH last_days AS (
  SELECT DISTINCT trade_date
  FROM cb_panel_pure_bond_ytm
  ORDER BY trade_date DESC
  LIMIT 60
),
ranked AS (
  SELECT
    y.trade_date,
    y.value,
    ROW_NUMBER() OVER (PARTITION BY y.trade_date ORDER BY y.value) AS rn,
    COUNT(*)    OVER (PARTITION BY y.trade_date) AS cnt
  FROM cb_panel_pure_bond_ytm y
  JOIN last_days d ON d.trade_date = y.trade_date
  WHERE y.value IS NOT NULL
),
picked AS (
  SELECT trade_date, value
  FROM ranked
  WHERE rn IN (FLOOR((cnt + 1)/2), FLOOR((cnt + 2)/2))
)
SELECT
  trade_date,
  AVG(value) AS median_value
FROM picked
GROUP BY trade_date
ON DUPLICATE KEY UPDATE median_value = VALUES(median_value);





--计算纯债溢价率中位数
SET @N := 60;

INSERT INTO cb_daily_median_premium_rate (trade_date, median_value)
WITH last_days AS (
  SELECT DISTINCT trade_date
  FROM cb_panel_pure_bond_premium_rate
  ORDER BY trade_date DESC
  LIMIT 60
),
ranked AS (
  SELECT
    y.trade_date,
    y.value,
    ROW_NUMBER() OVER (PARTITION BY y.trade_date ORDER BY y.value) AS rn,
    COUNT(*)    OVER (PARTITION BY y.trade_date) AS cnt
  FROM cb_panel_pure_bond_premium_rate y
  JOIN last_days d ON d.trade_date = y.trade_date
  WHERE y.value IS NOT NULL
),
picked AS (
  SELECT trade_date, value
  FROM ranked
  WHERE rn IN (FLOOR((cnt + 1)/2), FLOOR((cnt + 2)/2))
)
SELECT
  trade_date,
  AVG(value) AS median_value
FROM picked
GROUP BY trade_date
ON DUPLICATE KEY UPDATE median_value = VALUES(median_value);








-- MySQL 8.0.x
-- 条件价格指数（筛选后价格样本均值）
--
-- 剔除规则（满足任一条即不参与）：
--   (1) 价格 > 130 且 转股溢价率 > 50%
--   (2) 价格 > 120 且 转股溢价率 > 75%
--   (3) 价格 > 150
--   (4) 规模 < 3 亿元（cb_panel_scale.value 单位为亿元）
--   (5) 无评级，或评级低于 A+（保留 AAA / AA+ / AA / AA- ）
--
-- 使用前请先执行：schema_cb_conditional_price_index_mysql8.sql
--
-- ========== A) 每日更新（Wind 同步后执行，重算最近 30 个交易日）==========
SET @recent_days := 60;

INSERT INTO cb_daily_mean_price_cond (trade_date, mean_value)
WITH last_days AS (
  SELECT DISTINCT trade_date
  FROM cb_panel_price
  ORDER BY trade_date DESC
  LIMIT 60
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
    OR rating_norm NOT IN ('AAA', 'AA+', 'AA', 'AA-')
  )
)
SELECT
  trade_date,
  AVG(price) AS mean_value
FROM eligible
GROUP BY trade_date
ON DUPLICATE KEY UPDATE
  mean_value = VALUES(mean_value);
  
  















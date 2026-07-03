-- MySQL 8.0.x
-- 删除上市取消转债 123095.SZ（日升转债），并全量重算 4 张衍生指标表
-- 在 Navicat 中对 cb_data 库整段执行

SET @bond := '123095.SZ';

-- ========== 1) 删除 11 张原始面板表中的该券 ==========
DELETE FROM cb_panel_price                  WHERE bond_code = @bond;
DELETE FROM cb_panel_scale                  WHERE bond_code = @bond;
DELETE FROM cb_panel_conv_premium_rate      WHERE bond_code = @bond;
DELETE FROM cb_panel_turnover_rate          WHERE bond_code = @bond;
DELETE FROM cb_panel_pure_bond_premium_rate WHERE bond_code = @bond;
DELETE FROM cb_panel_bond_floor             WHERE bond_code = @bond;
DELETE FROM cb_panel_pure_bond_ytm          WHERE bond_code = @bond;
DELETE FROM cb_panel_rating                 WHERE bond_code = @bond;
DELETE FROM cb_panel_pct_chg                WHERE bond_code = @bond;
DELETE FROM cb_panel_remaining_term         WHERE bond_code = @bond;
DELETE FROM cb_panel_implied_vol            WHERE bond_code = @bond;

-- ========== 2) 删除衍生表（按券） ==========
DELETE FROM cb_panel_conv_value WHERE bond_code = @bond;

-- ========== 3) 全量重算 3 张日度中位数 + 转股价值 ==========
-- 说明：剔除 123095 后，各交易日横截面中位数可能变化，故对全历史重算

TRUNCATE TABLE cb_daily_median_price;

INSERT INTO cb_daily_median_price (trade_date, median_value)
WITH ranked AS (
  SELECT
    trade_date,
    value,
    ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY value) AS rn,
    COUNT(*) OVER (PARTITION BY trade_date) AS cnt
  FROM cb_panel_price
  WHERE value IS NOT NULL
),
picked AS (
  SELECT trade_date, value
  FROM ranked
  WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))
)
SELECT trade_date, AVG(value) AS median_value
FROM picked
GROUP BY trade_date;

TRUNCATE TABLE cb_daily_median_ytm;

INSERT INTO cb_daily_median_ytm (trade_date, median_value)
WITH ranked AS (
  SELECT
    trade_date,
    value,
    ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY value) AS rn,
    COUNT(*) OVER (PARTITION BY trade_date) AS cnt
  FROM cb_panel_pure_bond_ytm
  WHERE value IS NOT NULL
),
picked AS (
  SELECT trade_date, value
  FROM ranked
  WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))
)
SELECT trade_date, AVG(value) AS median_value
FROM picked
GROUP BY trade_date;

TRUNCATE TABLE cb_daily_median_premium_rate;

INSERT INTO cb_daily_median_premium_rate (trade_date, median_value)
WITH ranked AS (
  SELECT
    trade_date,
    value,
    ROW_NUMBER() OVER (PARTITION BY trade_date ORDER BY value) AS rn,
    COUNT(*) OVER (PARTITION BY trade_date) AS cnt
  FROM cb_panel_pure_bond_premium_rate
  WHERE value IS NOT NULL
),
picked AS (
  SELECT trade_date, value
  FROM ranked
  WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))
)
SELECT trade_date, AVG(value) AS median_value
FROM picked
GROUP BY trade_date;

TRUNCATE TABLE cb_panel_conv_value;

INSERT INTO cb_panel_conv_value (trade_date, bond_code, value)
SELECT
  p.trade_date,
  p.bond_code,
  CASE
    WHEN c.value IS NULL THEN NULL
    WHEN 1 + c.value / 100 = 0 THEN NULL
    ELSE p.value / (1 + c.value / 100)
  END AS conv_value
FROM cb_panel_price p
LEFT JOIN cb_panel_conv_premium_rate c
  ON c.trade_date = p.trade_date AND c.bond_code = p.bond_code;

-- ========== 4) 全量重算条件价格指数（若已建 cb_daily_mean_price_cond）==========
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
    AND p.bond_code <> @bond
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
SELECT trade_date, AVG(price) AS mean_value
FROM eligible
GROUP BY trade_date;

-- ========== 5) 验证 ==========
SELECT 'cb_panel_price' AS tbl, COUNT(*) AS n FROM cb_panel_price WHERE bond_code = @bond
UNION ALL SELECT 'cb_daily_median_price', COUNT(*) FROM cb_daily_median_price
UNION ALL SELECT 'cb_daily_median_ytm', COUNT(*) FROM cb_daily_median_ytm
UNION ALL SELECT 'cb_daily_median_premium_rate', COUNT(*) FROM cb_daily_median_premium_rate
UNION ALL SELECT 'cb_panel_conv_value', COUNT(*) FROM cb_panel_conv_value WHERE bond_code = @bond
UNION ALL SELECT 'cb_daily_mean_price_cond', COUNT(*) FROM cb_daily_mean_price_cond;

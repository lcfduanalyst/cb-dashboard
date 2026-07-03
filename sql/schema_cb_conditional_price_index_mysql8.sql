-- MySQL 8.0.x
-- 条件价格指数：按剔除规则筛选后的转债价格日度样本均值
-- 规模单位：亿元；无评级视为不达标并剔除
--
-- 若曾创建旧表 cb_daily_median_price_cond，可先执行：
--   DROP TABLE IF EXISTS cb_daily_median_price_cond;

CREATE TABLE IF NOT EXISTS cb_daily_mean_price_cond (
  trade_date   DATE NOT NULL PRIMARY KEY,
  mean_value   DECIMAL(20, 6) NULL COMMENT '条件筛选后价格样本均值'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

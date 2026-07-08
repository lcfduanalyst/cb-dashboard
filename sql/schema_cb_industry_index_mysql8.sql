-- MySQL 8.0.x
-- 可转债行业指数走势表（cb_strategy 库）

CREATE TABLE IF NOT EXISTS cb_industry_index (
  trade_date    DATE NOT NULL,
  industry_name VARCHAR(64) NOT NULL COMMENT '申万一级行业，含 _非银行 和 _全市场',
  index_value   DECIMAL(20, 6) NULL COMMENT '指数点位（2020-12-31 = 100）',
  daily_return  DECIMAL(20, 10) NULL COMMENT '当日涨跌幅（小数，0.01=1%）',
  market_cap    DECIMAL(24, 4) NULL COMMENT '前一日总市值（元）',
  bond_count    INT NULL COMMENT '当日样本数量',
  PRIMARY KEY (trade_date, industry_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

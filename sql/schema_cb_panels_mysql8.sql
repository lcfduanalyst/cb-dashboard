-- MySQL 8.0.x
-- 可转债面板（7个指标）表结构：统一为 (trade_date, bond_code) 主键，方便每日 UPSERT 覆盖更新
-- 建议库字符集：utf8mb4

CREATE TABLE IF NOT EXISTS cb_panel_scale (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL,
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_scale_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_conv_premium_rate (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL,
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_conv_premium_rate_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_price (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL,
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_price_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_turnover_rate (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL,
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_turnover_rate_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_pure_bond_premium_rate (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL,
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_pure_bond_premium_rate_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_bond_floor (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL,
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_bond_floor_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_pure_bond_ytm (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL,
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_pure_bond_ytm_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;


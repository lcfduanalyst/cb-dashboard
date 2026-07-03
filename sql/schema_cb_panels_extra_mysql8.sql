-- MySQL 8.0.x
-- 扩展基础数据表（评级、日涨跌幅、剩余期限、隐含波动率）
-- 与 cb_panel_price 键对齐：(trade_date, bond_code) 主键，便于每日 UPSERT

CREATE TABLE IF NOT EXISTS cb_panel_rating (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value_text VARCHAR(32) NULL COMMENT '主体信用评级 latestissurercreditrating',
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_rating_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_pct_chg (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL COMMENT '日涨跌幅 pct_chg',
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_pct_chg_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_remaining_term (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL COMMENT '剩余期限(年) ptmyear',
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_remaining_term_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS cb_panel_implied_vol (
  trade_date DATE NOT NULL,
  bond_code  VARCHAR(20) NOT NULL,
  value      DECIMAL(20, 6) NULL COMMENT '隐含波动率 impliedvol（wss 需 rfIndex=1）',
  PRIMARY KEY (trade_date, bond_code),
  KEY idx_cb_panel_implied_vol_bond_date (bond_code, trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

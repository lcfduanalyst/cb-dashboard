-- MySQL 8.0.x
-- 可转债基础信息（截面快照）：与 转债基础信息.xlsx 列一致，主键 bond_code
-- 日常由 wind_sync_cb_panels.py 经 Wind w.wss 更新；可用 --import-bond-info 从 Excel 灌入/覆盖

CREATE TABLE IF NOT EXISTS cb_bond_info (
  bond_code        VARCHAR(20)  NOT NULL COMMENT '证券代码 Wind XXXXXX.SH/SZ',
  bond_name        VARCHAR(64)  NULL COMMENT '证券简称',
  list_date        DATE         NULL COMMENT '上市日期',
  last_trade_date  DATE         NULL COMMENT '最后交易日（在市为空）',
  stock_code       VARCHAR(20)  NULL COMMENT '正股代码',
  stock_name       VARCHAR(64)  NULL COMMENT '正股简称',
  sw_industry_l1   VARCHAR(64)  NULL COMMENT '申万一级行业',
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (bond_code),
  KEY idx_cb_bond_info_stock (stock_code),
  KEY idx_cb_bond_info_list_date (list_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

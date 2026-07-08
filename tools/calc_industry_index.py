#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可转债行业指数计算：以 2020-12-31 为基日（基点 100），按申万一级行业
加权计算每日指数走势，同时计算非银行和全市场汇总指数。

加权方式：前一日个债存续规模 × 前一日收盘价 / 100 = 个债市值
         → 行业当日涨跌幅 = Σ(个债市值 × 当日涨跌幅) / Σ(个债市值)
         → 指数 = 前一日指数 × (1 + 当日涨跌幅)

样本条件：前一日已上市且存续、规模 > 0.3 亿、当日非最后交易日

数据源：
  cb_data.cb_bond_info     → sw_industry_l1 / list_date / last_trade_date
  cb_data.cb_panel_price   → close
  cb_data.cb_panel_scale   → outstandingbalance（亿元）
  cb_data.cb_panel_pct_chg → 涨跌幅（%）

输出：cb_strategy.cb_industry_index

用法：
  # 首次：全量计算 2020-12-31 ~ 今天
  python tools/calc_industry_index.py --write

  # 每日更新（只算最近 N 天）
  python tools/calc_industry_index.py --write --lookback 30

  # 指定日期区间
  python tools/calc_industry_index.py --start-date 2025-01-01 --end-date 2025-06-30 --write
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import pymysql

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 常量 ─────────────────────────────────────────────────
BASE_DATE = date(2020, 12, 31)
BASE_VALUE = 100.0
MIN_SCALE_YI = 0.3  # 最小存续规模（亿元）
TABLE_NAME = "cb_industry_index"
DATABASE = "cb_strategy"
NON_BANK_KEY = "_非银行"
ALL_MARKET_KEY = "_全市场"


# ── 数据库 ───────────────────────────────────────────────
def mysql_connect(
    host: str, port: int, user: str, password: str, database: str
) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, charset="utf8mb4",
    )


def sql_col_ident(name: str) -> str:
    return f"`{name}`" if name.isdigit() else name


# ── 数据加载 ─────────────────────────────────────────────
def load_bond_info(conn) -> pd.DataFrame:
    """加载转债基础信息：行业、上市日、最后交易日。"""
    sql = """
    SELECT bond_code, sw_industry_l1 AS industry,
           list_date, last_trade_date
    FROM cb_data.cb_bond_info
    """
    df = pd.read_sql(sql, conn)
    df["list_date"] = pd.to_datetime(df["list_date"])
    df["last_trade_date"] = pd.to_datetime(df["last_trade_date"])
    return df


def load_panel_data(conn, table: str, start: str, end: str) -> pd.DataFrame:
    """加载面板表数据 (trade_date, bond_code, value)。"""
    sql = f"""
    SELECT trade_date, bond_code, value
    FROM cb_data.{table}
    WHERE trade_date BETWEEN %s AND %s
      AND value IS NOT NULL
    """
    df = pd.read_sql(sql, conn, params=(start, end))
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna(subset=["value"])


# ── 指数计算核心 ─────────────────────────────────────────
def compute_industry_index(
    bonds: pd.DataFrame,
    prices: pd.DataFrame,
    scales: pd.DataFrame,
    pct_chgs: pd.DataFrame,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """
    以价格表为锚，计算每日行业加权指数。

    步骤：
      1. 以 price 为主表，LEFT JOIN scale、行业属性、list_date/last_trade_date
      2. 每个 bond_code 内，用 shift(-1) 匹配「当日 pct_chg → 前一日市值」
      3. market_cap = scale × price / 100
      4. value_change = market_cap × pct_chg_next / 100
      5. 按 (next_trade_date, industry) 汇总 → 涨跌幅 → 累计指数
    """
    # ── Step 1: 以 price 为锚，合并 scale + 行业 ──
    df = prices.rename(columns={"value": "close"}).merge(
        scales.rename(columns={"value": "scale"}),
        on=["trade_date", "bond_code"],
        how="left",
    )
    df = df.merge(
        bonds[["bond_code", "industry", "list_date", "last_trade_date"]],
        on="bond_code",
        how="left",
    )
    df = df.dropna(subset=["industry"])
    df["scale"] = pd.to_numeric(df["scale"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["scale", "close"])

    # ── Step 2: 用 shift(-1) 匹配实际下一个交易日的涨跌幅 ──
    pct = pct_chgs.rename(columns={"value": "pct_chg", "trade_date": "pct_date"}).copy()
    pct["pct_chg"] = pd.to_numeric(pct["pct_chg"], errors="coerce")
    pct = pct.dropna(subset=["pct_chg"])

    # 对每个 bond_code，pct_date 就是下一交易日的日期
    # 把主表按 bond_code 分组，下一行的 trade_date 就是「下一个交易日」
    df = df.sort_values(["bond_code", "trade_date"])
    df["next_trade_date"] = df.groupby("bond_code")["trade_date"].shift(-1)

    # 用 next_trade_date + bond_code 去匹配 pct 表的 pct_date + bond_code
    df = df.merge(
        pct,
        left_on=["next_trade_date", "bond_code"],
        right_on=["pct_date", "bond_code"],
        how="inner",
    )

    # ── Step 3: 过滤 ──
    # (a) 规模 > 0.3 亿
    df = df[df["scale"] > MIN_SCALE_YI]
    # (b) 前一日已上市
    df = df[df["list_date"].notna() & (df["list_date"] <= df["trade_date"])]
    # (c) 当日不是最后交易日
    df = df[
        df["last_trade_date"].isna()
        | (df["last_trade_date"] != df["next_trade_date"])
    ]
    # (d) 排除极端涨跌幅
    df = df[(df["pct_chg"] > -100) & (df["pct_chg"] < 100)]

    if df.empty:
        return pd.DataFrame()

    # ── Step 4: 计算个债市值 + 市值变动 ──
    # market_cap = scale(亿元) × close / 100 → 单位：亿元市值
    df["market_cap"] = df["scale"] * df["close"] / 100.0
    df["value_change"] = df["market_cap"] * df["pct_chg"] / 100.0

    # ── Step 5: 按 (next_trade_date, industry) 汇总 ──
    grp = df.groupby(["next_trade_date", "industry"], sort=True)
    daily = grp.agg(
        total_market_cap=("market_cap", "sum"),
        total_value_change=("value_change", "sum"),
        bond_count=("bond_code", "count"),
    ).reset_index()
    daily.rename(columns={"next_trade_date": "trade_date"}, inplace=True)
    daily["daily_return"] = daily["total_value_change"] / daily["total_market_cap"]
    daily = daily.dropna(subset=["daily_return"])

    if daily.empty:
        return pd.DataFrame()

    # ── Step 6: 非银行 & 全市场汇总 ──
    def _aggregate(sub_df: pd.DataFrame, label: str) -> pd.DataFrame:
        agg = sub_df.groupby("next_trade_date", sort=True).agg(
            total_market_cap=("market_cap", "sum"),
            total_value_change=("value_change", "sum"),
            bond_count=("bond_code", "count"),
        ).reset_index()
        agg.rename(columns={"next_trade_date": "trade_date"}, inplace=True)
        agg["daily_return"] = agg["total_value_change"] / agg["total_market_cap"]
        agg["industry"] = label
        return agg.dropna(subset=["daily_return"])

    non_bank = _aggregate(df[df["industry"] != "银行"], NON_BANK_KEY)
    all_mkt = _aggregate(df, ALL_MARKET_KEY)
    cols = ["trade_date", "industry", "daily_return", "total_market_cap", "bond_count"]
    result = pd.concat([daily[cols], non_bank[cols], all_mkt[cols]], ignore_index=True)

    # ── Step 7: 计算累计指数（基日之后） ──
    result = result.sort_values(["industry", "trade_date"])
    result = result[result["trade_date"] > pd.Timestamp(BASE_DATE)]
    if result.empty:
        return result

    result["index_value"] = np.nan
    for ind, grp in result.groupby("industry"):
        mask = result["industry"] == ind
        rets = 1.0 + grp.sort_values("trade_date")["daily_return"].values
        result.loc[mask, "index_value"] = BASE_VALUE * np.cumprod(rets)

    result = result.dropna(subset=["index_value"])
    return result.reset_index(drop=True)


# ── 数据库写入 ───────────────────────────────────────────
def upsert_index(conn, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    sql = f"""
    INSERT INTO {DATABASE}.{TABLE_NAME}
      (trade_date, industry_name, index_value, daily_return, market_cap, bond_count)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
      index_value = VALUES(index_value),
      daily_return = VALUES(daily_return),
      market_cap   = VALUES(market_cap),
      bond_count   = VALUES(bond_count)
    """
    rows = []
    ind_col = "industry" if "industry" in df.columns else df.columns[1]
    for _, r in df.iterrows():
        rows.append((
            r["trade_date"].date() if hasattr(r["trade_date"], "date") else r["trade_date"],
            str(r[ind_col]),
            float(r["index_value"]) if not pd.isna(r["index_value"]) else None,
            float(r["daily_return"]) if not pd.isna(r["daily_return"]) else None,
            float(r["total_market_cap"]) if not pd.isna(r["total_market_cap"]) else None,
            int(r["bond_count"]) if not pd.isna(r["bond_count"]) else None,
        ))
    with conn.cursor() as cur:
        cur.executemany(sql, rows)
    conn.commit()
    return len(rows)


# ── 建表 ────────────────────────────────────────────────
def ensure_table(conn) -> None:
    schema_file = _PROJECT_ROOT / "sql" / "schema_cb_industry_index_mysql8.sql"
    if schema_file.exists():
        sql = schema_file.read_text(encoding="utf-8")
        # 替换库名前缀
        parts = [p.strip() for p in sql.split(";") if p.strip()]
        with conn.cursor() as cur:
            for p in parts:
                cur.execute(p)
        conn.commit()


# ── 主流程 ───────────────────────────────────────────────
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="可转债行业指数计算")
    parser.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "161106"))
    parser.add_argument("--start-date", default=None, help="开始日期，默认基日次日")
    parser.add_argument("--end-date", default=None, help="结束日期，默认今天")
    parser.add_argument("--lookback", type=int, default=0,
                        help="回溯天数（如 --lookback 30 只算最近30天）；0=全部")
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="正式写入数据库")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    today = date.today()

    if args.end_date is None:
        args.end_date = today.strftime("%Y-%m-%d")
    if args.start_date is None:
        if args.lookback > 0:
            start_d = today - timedelta(days=args.lookback * 2)  # 多取一些覆盖非交易日
            args.start_date = start_d.strftime("%Y-%m-%d")
        else:
            args.start_date = BASE_DATE.strftime("%Y-%m-%d")

    print(f"行业指数计算: {args.start_date} ~ {args.end_date}")
    print(f"基日: {BASE_DATE}, 基点: {BASE_VALUE}")
    print(f"模式: {'DRY-RUN' if args.dry_run else 'WRITE'}")

    # 连接 cb_data（读取）+ cb_strategy（写入）
    conn_data = mysql_connect(args.host, args.port, args.user, args.password, "cb_data")
    conn_strategy = mysql_connect(args.host, args.port, args.user, args.password, DATABASE)

    try:
        # 建表
        ensure_table(conn_strategy)

        # 加载数据
        print("加载基础信息...")
        bonds = load_bond_info(conn_data)
        print(f"  转债数量: {len(bonds)}")
        print(f"  行业分布: {bonds['industry'].nunique()} 个行业")

        # 价格/规模/涨跌幅从基日前一天开始取（需要 T-1 数据）
        data_start = (
            pd.Timestamp(args.start_date) - pd.Timedelta(days=10)
        ).strftime("%Y-%m-%d")
        print(f"加载面板数据 ({data_start} ~ {args.end_date})...")
        prices = load_panel_data(conn_data, "cb_panel_price", data_start, args.end_date)
        scales = load_panel_data(conn_data, "cb_panel_scale", data_start, args.end_date)
        pct_chgs = load_panel_data(conn_data, "cb_panel_pct_chg", data_start, args.end_date)
        print(f"  price: {len(prices)} 行, scale: {len(scales)} 行, pct_chg: {len(pct_chgs)} 行")

        # 计算
        print("计算行业指数...")
        result = compute_industry_index(
            bonds, prices, scales, pct_chgs,
            start_date=pd.Timestamp(args.start_date).date(),
            end_date=pd.Timestamp(args.end_date).date(),
        )

        if result.empty:
            print("无有效计算结果")
            return 1

        print(f"计算结果: {len(result)} 行")
        print(f"日期范围: {result['trade_date'].min().date()} ~ {result['trade_date'].max().date()}")
        cols = result.columns.tolist()
        ind_col = "industry" if "industry" in cols else cols[1]
        print(f"行业/分组: {result[ind_col].nunique()} 个")

        # 展示最新一日
        latest_date = result["trade_date"].max()
        latest = result[result["trade_date"] == latest_date].sort_values("index_value", ascending=False)
        print(f"\n=== {latest_date.date()} 各行业指数 ===")
        print(f"{'行业':14s} {'指数':>8s} {'涨跌幅':>8s} {'样本':>6s}")
        print("-" * 40)
        for _, r in latest.iterrows():
            ret_pct = float(r["daily_return"]) * 100 if not pd.isna(r["daily_return"]) else 0.0
            print(
                f"{str(r[ind_col]):14s} {float(r['index_value']):>8.2f} "
                f"{ret_pct:>7.2f}% {int(r['bond_count']):>5d}"
            )

        # 写入
        if args.dry_run:
            print("\n[Dry-run] 未写入数据库")
        else:
            n = upsert_index(conn_strategy, result)
            print(f"\n写入 {n} 行到 {DATABASE}.{TABLE_NAME}")
    finally:
        conn_data.close()
        conn_strategy.close()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

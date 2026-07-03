#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百元溢价率（对数指数拟合）：按交易日截面回归，写入 cb_daily_premium_valuation。

公式（与 premium_100_2.py、报告一致）：
  close = c * (a + beta * ln(1 + exp((x-a)/beta))) + (1-c) * x
  premium_ratio = fitted_price(x) / x - 1   （表内存小数，如 0.25 表示 25%）

表结构同 溢价率估值.xlsx：trade_date, c, a, b, 60..150

示例：
  # PyCharm 直接运行：修改 DEFAULT_START_DATE / DEFAULT_END_DATE
  python tools/run_premium_by_conv_value.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import numpy as np
import pandas as pd
import pymysql
from scipy.optimize import leastsq

from wind_panel_registry import EXCLUDED_BOND_CODES

TABLE_NAME = "cb_daily_premium_valuation"
CONV_GRID_MIN = 60
CONV_GRID_MAX = 150
COEF_COLS = ("c", "a", "b")
PREM_COLS: List[str] = [str(i) for i in range(CONV_GRID_MIN, CONV_GRID_MAX + 1)]
DB_COLUMNS: List[str] = ["trade_date", *COEF_COLS, *PREM_COLS]

CONV_STEP_DEFAULT = 1
MIN_SAMPLE = 6
REGRESS_INITIAL = np.array([1.0, 100.0, 1.0])

# PyCharm 直接运行时改这里
DEFAULT_START_DATE = "2026-06-29"
DEFAULT_END_DATE = "2026-07-02"

CROSS_SECTION_SQL = """
SELECT
  p.trade_date,
  p.bond_code,
  p.value AS close,
  cv.value AS convert_value,
  t.value AS turn,
  s.value AS remain_amount,
  c.value AS convert_premium_ratio
FROM cb_panel_price p
INNER JOIN cb_panel_conv_value cv
  ON cv.trade_date = p.trade_date AND cv.bond_code = p.bond_code
LEFT JOIN cb_panel_turnover_rate t
  ON t.trade_date = p.trade_date AND t.bond_code = p.bond_code
LEFT JOIN cb_panel_scale s
  ON s.trade_date = p.trade_date AND s.bond_code = p.bond_code
LEFT JOIN cb_panel_conv_premium_rate c
  ON c.trade_date = p.trade_date AND c.bond_code = p.bond_code
WHERE p.trade_date BETWEEN %s AND %s
  AND p.value IS NOT NULL
  AND cv.value IS NOT NULL
  AND p.bond_code NOT IN ({excluded})
ORDER BY p.trade_date, p.bond_code
"""


def sql_ident(name: str) -> str:
    return f"`{name}`" if name.isdigit() else name


def build_upsert_sql() -> str:
    cols_sql = ", ".join(sql_ident(c) for c in DB_COLUMNS)
    placeholders = ", ".join(["%s"] * len(DB_COLUMNS))
    updates = ", ".join(
        f"{sql_ident(c)}=VALUES({sql_ident(c)})"
        for c in DB_COLUMNS
        if c != "trade_date"
    )
    return (
        f"INSERT INTO {TABLE_NAME} ({cols_sql}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )


def _to_db_value(v: object) -> object:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date()
    if hasattr(v, "year") and hasattr(v, "month") and hasattr(v, "day"):
        return v
    return v


def dataframe_to_rows(df: pd.DataFrame) -> List[tuple]:
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    for col in DB_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out[DB_COLUMNS]
    rows: List[tuple] = []
    for row in out.itertuples(index=False, name=None):
        rows.append(tuple(_to_db_value(v) for v in row))
    return rows


def upsert_dataframe(
    conn: pymysql.connections.Connection,
    df: pd.DataFrame,
    batch_size: int = 200,
) -> int:
    if df.empty:
        return 0
    sql = build_upsert_sql()
    rows = dataframe_to_rows(df)
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch_size):
            cur.executemany(sql, rows[i : i + batch_size])
    conn.commit()
    return len(rows)


def merge_coef_and_premium(
    coef_df: pd.DataFrame,
    prem_wide: pd.DataFrame,
    prem_as_ratio: bool = True,
) -> pd.DataFrame:
    """coef_df: beta,a,c → 表列 c,a,b；prem_wide 列 60..150。"""
    out = coef_df[["trade_date"]].copy()
    out["c"] = coef_df["c"]
    out["a"] = coef_df["a"]
    out["b"] = coef_df["beta"]
    for col in PREM_COLS:
        if col in prem_wide.columns:
            vals = pd.to_numeric(prem_wide[col], errors="coerce")
            out[col] = vals / 100.0 if prem_as_ratio else vals
        else:
            out[col] = np.nan
    return out


def mysql_connect(
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
    )


def conv_levels(min_v: int, max_v: int, step: int) -> np.ndarray:
    return np.arange(min_v, max_v + 1, step)


def cal_coefs(coef: np.ndarray, x: np.ndarray, y: np.ndarray) -> List[float]:
    beta, a, c = coef
    err = list(
        y
        - (
            c * (a + beta * np.log(1 + np.exp((x - a) / beta)))
            + (1 - c) * x
        )
    )
    err.append(max(0 - c, 0) * 1e8)
    return err


def fitted_price(x: float, beta: float, a: float, c: float) -> float:
    return float(
        c * (a + beta * np.log(1 + np.exp((x - a) / beta))) + (1 - c) * x
    )


def premium_pct(x: float, beta: float, a: float, c: float) -> float:
    return (fitted_price(x, beta, a, c) / x - 1.0) * 100.0


def regress_day(df_day: pd.DataFrame, min_sample: int = MIN_SAMPLE) -> Tuple[float, float, float]:
    if len(df_day) < min_sample:
        return float("nan"), float("nan"), float("nan")
    x = df_day["convert_value"].to_numpy(dtype=float)
    y = df_day["close"].to_numpy(dtype=float)
    try:
        coef, _ = leastsq(
            cal_coefs,
            REGRESS_INITIAL.copy(),
            args=(x, y),
            maxfev=200,
        )
        return float(coef[0]), float(coef[1]), float(coef[2])
    except Exception:
        return float("nan"), float("nan"), float("nan")


def filter_sample(df: pd.DataFrame) -> pd.DataFrame:
    mask = (
        (df["turn"] > 0)
        & (df["turn"] <= 40)
        & (df["remain_amount"] > 2)
        & df["convert_premium_ratio"].notna()
        & (df["convert_premium_ratio"] > -5)
        & (df["convert_value"] >= 70)
        & (df["convert_value"] <= 140)
        & df["close"].notna()
        & df["convert_value"].notna()
    )
    return df.loc[mask].copy()


def excluded_placeholders(codes: Iterable[str]) -> str:
    items = sorted({str(c).strip().upper() for c in codes if str(c).strip()})
    if not items:
        return "''"
    return ", ".join(f"'{c}'" for c in items)


def load_cross_section(
    conn: pymysql.connections.Connection,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    sql = CROSS_SECTION_SQL.format(excluded=excluded_placeholders(EXCLUDED_BOND_CODES))
    df = pd.read_sql(sql, conn, params=(start_date, end_date))
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in ("close", "convert_value", "turn", "remain_amount", "convert_premium_ratio"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def process_range(
    cross_df: pd.DataFrame,
    levels: np.ndarray,
    min_sample: int = MIN_SAMPLE,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    prem_rows: List[dict] = []
    coef_rows: List[dict] = []

    for trade_date, group in cross_df.groupby("trade_date", sort=True):
        sample = filter_sample(group)
        beta, a, c = regress_day(sample, min_sample=min_sample)

        prem_row: dict = {"trade_date": trade_date}
        for x in levels:
            col = str(int(x))
            if np.isnan(beta):
                prem_row[col] = np.nan
            else:
                prem_row[col] = premium_pct(float(x), beta, a, c)

        prem_rows.append(prem_row)
        coef_rows.append(
            {
                "trade_date": trade_date,
                "beta": beta,
                "a": a,
                "c": c,
                "n_sample": len(sample),
            }
        )

    return pd.DataFrame(prem_rows), pd.DataFrame(coef_rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="百元溢价率：截面回归 → UPSERT cb_daily_premium_valuation。"
    )
    parser.add_argument(
        "--start-date",
        default=DEFAULT_START_DATE,
        help=f"起始日期，默认 {DEFAULT_START_DATE}",
    )
    parser.add_argument(
        "--end-date",
        default=DEFAULT_END_DATE,
        help=f"结束日期，默认 {DEFAULT_END_DATE}",
    )
    parser.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "161106"))
    parser.add_argument("--db", dest="database", default=os.getenv("MYSQL_DB", "cb_data"))
    parser.add_argument(
        "--conv-min",
        type=int,
        default=CONV_GRID_MIN,
        help=f"转股价值网格下限，默认 {CONV_GRID_MIN}",
    )
    parser.add_argument(
        "--conv-max",
        type=int,
        default=CONV_GRID_MAX,
        help=f"转股价值网格上限，默认 {CONV_GRID_MAX}",
    )
    parser.add_argument(
        "--conv-step",
        type=int,
        default=CONV_STEP_DEFAULT,
        help=f"步长，默认 {CONV_STEP_DEFAULT}",
    )
    parser.add_argument(
        "--min-sample",
        type=int,
        default=MIN_SAMPLE,
        help=f"回归最少样本数，默认 {MIN_SAMPLE}",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    levels = conv_levels(args.conv_min, args.conv_max, args.conv_step)

    print(f"MySQL: {args.user}@{args.host}:{args.port}/{args.database}")
    print(f"Table: {TABLE_NAME}")
    print(f"Range: {args.start_date} ~ {args.end_date}")
    print(
        f"Conv grid: {args.conv_min}..{args.conv_max} step {args.conv_step} "
        f"({len(levels)} levels)"
    )

    conn = mysql_connect(
        args.host, args.port, args.user, args.password, args.database
    )
    try:
        cross_df = load_cross_section(conn, args.start_date, args.end_date)
        if cross_df.empty:
            raise SystemExit(
                "未读到截面数据。请确认 cb_panel_price、cb_panel_conv_value 等有数据。"
            )

        n_dates = cross_df["trade_date"].nunique()
        print(f"Loaded rows={len(cross_df)}  trade_dates={n_dates}")

        prem_wide, coef_df = process_range(
            cross_df, levels, min_sample=args.min_sample
        )

        ok_coef = coef_df["beta"].notna().sum()
        print(f"Regressed days: {ok_coef}/{len(coef_df)}")
        if ok_coef:
            latest = coef_df.dropna(subset=["beta"]).iloc[-1]
            prem100 = prem_wide.loc[prem_wide["trade_date"] == latest["trade_date"], "100"]
            p100 = float(prem100.iloc[0]) if len(prem100) else float("nan")
            print(
                f"Latest date={pd.Timestamp(latest['trade_date']).date()} "
                f"c={latest['c']:.4f} a={latest['a']:.4f} b={latest['beta']:.4f} "
                f"n={int(latest['n_sample'])} premium_100={p100:.2f}%"
            )

        db_df = merge_coef_and_premium(coef_df, prem_wide, prem_as_ratio=True)
        n = upsert_dataframe(conn, db_df)
        print(f"\nUpserted {n} rows into {TABLE_NAME}")
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    main()

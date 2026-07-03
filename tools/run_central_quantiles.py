#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中枢序列 + 扩展窗口历史分位数（q25/q50/q75）→ Excel / 可选 PNG。

从 MySQL 读取日度中枢，对每个交易日用「截至当日的全部历史」计算分位数，
并计算当日 value 在指定区间内的历史分位排名（pct_2017 / pct_2022），
全量重算后写入 Excel（完整时间序列，每指标一个 sheet）。

数据源：
  - cb_daily_median_premium_rate.median_value  纯债溢价率中枢
  - cb_daily_median_price.median_value         全样本价格中枢
  - cb_daily_median_ytm.median_value           纯债YTM中枢
  - cb_daily_mean_price_cond.mean_value        条件价格中枢（样本均值）
  - cb_daily_premium_valuation.`100`           百元转股溢价率估值（×100 为 %）

Excel 每指标 sheet 列：
  trade_date, value, q25, q50, q75, pct_2017, pct_2022
  pct_2017 / pct_2022：当日 value 在 2017-01-01 / 2022-01-01 以来历史中的分位（0~100）

示例：
  python tools/run_central_quantiles.py --write-excel
  python tools/run_central_quantiles.py --write-excel --plot
  python tools/run_central_quantiles.py --output output/central_quantiles.xlsx --plot
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "SimSun"]
plt.rcParams["axes.unicode_minus"] = False  # 负号正常显示

import numpy as np
import pandas as pd
import pymysql

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT_DIR = _PROJECT_ROOT / "output" / "central_quantiles"

# 历史分位排名窗口：(起始日, 输出列名)
HIST_PCT_WINDOWS: Tuple[Tuple[str, str], ...] = (
    ("2017-01-01", "pct_2017"),
    ("2022-01-01", "pct_2022"),
)

EXCEL_EXPORT_COLS: Tuple[str, ...] = (
    "trade_date",
    "value",
    "q25",
    "q50",
    "q75",
    "pct_2017",
    "pct_2022",
)


@dataclass(frozen=True)
class MetricSpec:
    metric_id: str
    sheet_name: str
    table: str
    value_col: str
    plot_title: str
    value_scale: float = 1.0
    ylabel: str = "数值"
    series_label: str = "中枢"


def sql_col_ident(name: str) -> str:
    return f"`{name}`" if name.isdigit() else name


METRICS: Tuple[MetricSpec, ...] = (
    MetricSpec(
        "premium",
        "纯债溢价率中枢",
        "cb_daily_median_premium_rate",
        "median_value",
        "纯债溢价率中枢",
    ),
    MetricSpec(
        "price",
        "全样本价格中枢",
        "cb_daily_median_price",
        "median_value",
        "全样本转债价格中枢",
    ),
    MetricSpec(
        "ytm",
        "纯债YTM中枢",
        "cb_daily_median_ytm",
        "median_value",
        "纯债YTM中枢",
    ),
    MetricSpec(
        "cond_mean",
        "条件价格中枢",
        "cb_daily_mean_price_cond",
        "mean_value",
        "条件价格指数",
    ),
    MetricSpec(
        "premium_100",
        "百元转股溢价率",
        "cb_daily_premium_valuation",
        "100",
        "百元转股溢价率及历史分位数",
        value_scale=100.0,
        ylabel="溢价率(%)",
        series_label="百元溢价率",
    ),
)


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


def load_series(conn: pymysql.connections.Connection, spec: MetricSpec) -> pd.DataFrame:
    col = sql_col_ident(spec.value_col)
    sql = f"""
    SELECT trade_date, {col} AS value
    FROM {spec.table}
    WHERE {col} IS NOT NULL
    ORDER BY trade_date
    """
    df = pd.read_sql(sql, conn)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    if spec.value_scale != 1.0:
        df["value"] = df["value"] * spec.value_scale
    df = df.dropna(subset=["value"]).sort_values("trade_date").reset_index(drop=True)
    return df


def add_expanding_quantiles(df: pd.DataFrame) -> pd.DataFrame:
    """截至每个交易日的历史 q25/q50/q75。"""
    out = df.copy()
    s = out["value"]
    out["q25"] = s.expanding(min_periods=1).quantile(0.25)
    out["q50"] = s.expanding(min_periods=1).quantile(0.50)
    out["q75"] = s.expanding(min_periods=1).quantile(0.75)
    return out


def add_historical_percentile_ranks(
    df: pd.DataFrame,
    windows: Tuple[Tuple[str, str], ...] = HIST_PCT_WINDOWS,
    as_percent: bool = True,
) -> pd.DataFrame:
    """每个时点：在 [window_start, trade_date] 内计算当日 value 的历史分位排名。"""
    out = df.copy()
    dates = out["trade_date"]
    values = out["value"].to_numpy(dtype=float)
    n = len(out)

    for start_str, col_name in windows:
        start = pd.Timestamp(start_str)
        ranks = np.full(n, np.nan, dtype=float)

        for i in range(n):
            if dates.iloc[i] < start:
                continue
            mask = (dates >= start) & (dates <= dates.iloc[i])
            window = values[mask]
            if window.size == 0:
                continue
            ranks[i] = np.mean(window <= values[i])

        if as_percent:
            ranks = ranks * 100.0
        out[col_name] = ranks

    return out


def enrich_metric_series(df: pd.DataFrame) -> pd.DataFrame:
    out = add_expanding_quantiles(df)
    out = add_historical_percentile_ranks(out)
    return out


def plot_metric(df: pd.DataFrame, spec: MetricSpec, save_path: Path) -> None:
    save_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(
        df["trade_date"],
        df["value"],
        label=spec.series_label,
        linewidth=1.4,
        color="#2563eb",
    )
    ax.plot(df["trade_date"], df["q25"], "--", linewidth=1.0, label="历史 q25", color="#94a3b8")
    ax.plot(df["trade_date"], df["q50"], "--", linewidth=1.0, label="历史 q50", color="#64748b")
    ax.plot(df["trade_date"], df["q75"], "--", linewidth=1.0, label="历史 q75", color="#475569")
    ax.set_title(spec.plot_title)
    ax.set_xlabel("日期")
    ax.set_ylabel(spec.ylabel)
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def write_excel(path: Path, sheets: Dict[str, pd.DataFrame]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, df in sheets.items():
            cols = [c for c in EXCEL_EXPORT_COLS if c in df.columns]
            out = df[cols].copy()
            out["trade_date"] = out["trade_date"].dt.strftime("%Y-%m-%d")
            safe_name = sheet_name[:31]
            out.to_excel(writer, sheet_name=safe_name, index=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="中枢序列扩展窗口分位数：全量重算 → Excel / 可选 PNG。"
    )
    parser.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "161106"))
    parser.add_argument("--db", dest="database", default=os.getenv("MYSQL_DB", "cb_data"))
    parser.add_argument(
        "--metrics",
        default="all",
        help="指标 id，逗号分隔；默认 all。可选：premium,price,ytm,cond_mean,premium_100",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_DEFAULT_OUT_DIR,
        help=f"输出目录，默认 {_DEFAULT_OUT_DIR}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Excel 路径；默认 output-dir/central_quantiles_YYYYMMDD.xlsx",
    )
    parser.add_argument(
        "--also-fixed-name",
        action="store_true",
        help="同时写入 output-dir/central_quantiles.xlsx（覆盖最新快照）",
    )
    parser.add_argument("--write-excel", action="store_true", help="写入 Excel")
    parser.add_argument("--plot", action="store_true", help="生成 PNG 图")
    args = parser.parse_args()
    if not args.write_excel and not args.plot:
        args.write_excel = True
        args.plot = True  # 新增这一行
    return args


def select_metrics(raw: str) -> List[MetricSpec]:
    by_id = {m.metric_id: m for m in METRICS}
    if raw.strip().lower() == "all":
        return list(METRICS)
    ids = [x.strip() for x in raw.split(",") if x.strip()]
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise SystemExit(f"未知指标：{unknown}。可选：{list(by_id.keys())}")
    return [by_id[i] for i in ids]


def main() -> None:
    args = parse_args()
    specs = select_metrics(args.metrics)

    out_dir = args.output_dir
    chart_dir = out_dir / "charts"
    xlsx_path = args.output or (out_dir / f"central_quantiles_{date.today():%Y%m%d}.xlsx")

    print(f"MySQL: {args.user}@{args.host}:{args.port}/{args.database}")
    print(f"Metrics: {[s.metric_id for s in specs]}")
    if args.write_excel:
        print(f"Excel: {xlsx_path}")
    if args.plot:
        print(f"Charts: {chart_dir}")

    conn = mysql_connect(
        args.host, args.port, args.user, args.password, args.database
    )
    sheets: Dict[str, pd.DataFrame] = {}
    try:
        for spec in specs:
            print(f"\n==> {spec.plot_title}")
            df = load_series(conn, spec)
            if df.empty:
                print(f"    无数据（表 {spec.table}），跳过")
                continue
            result = enrich_metric_series(df)
            sheets[spec.sheet_name] = result
            latest = result.iloc[-1]
            print(
                f"    rows={len(result)}  latest={latest['trade_date'].date()} "
                f"value={latest['value']:.4f} q50={latest['q50']:.4f} "
                f"pct_2017={latest['pct_2017']:.2f} pct_2022={latest['pct_2022']:.2f}"
            )
            if args.plot:
                png = chart_dir / f"{spec.metric_id}.png"
                plot_metric(result, spec, png)
                print(f"    chart -> {png}")
    finally:
        conn.close()

    if not sheets:
        raise SystemExit("没有可输出的指标数据，请确认 MySQL 衍生表已有数据。")

    if args.write_excel:
        write_excel(xlsx_path, sheets)
        print(f"\nWrote Excel: {xlsx_path}")
        if args.also_fixed_name:
            fixed = out_dir / "central_quantiles.xlsx"
            write_excel(fixed, sheets)
            print(f"Wrote Excel: {fixed}")

    print("\nDone.")


if __name__ == "__main__":
    main()

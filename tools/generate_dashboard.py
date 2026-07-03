#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成可转债数据跟踪 HTML 看板（自包含单文件，浏览器直接打开）。

数据源（同 run_central_quantiles.py）：
  - cb_daily_median_premium_rate  纯债溢价率中枢
  - cb_daily_median_price         全样本价格中枢
  - cb_daily_median_ytm           纯债 YTM 中枢
  - cb_daily_mean_price_cond      条件价格中枢

用法：
  python tools/generate_dashboard.py
  python tools/generate_dashboard.py --output output/dashboard/cb_dashboard.html
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import pymysql

# pandas + pymysql 直接使用会有 SQLAlchemy 警告，抑制即可
warnings.filterwarnings("ignore", message=".*pandas only supports SQLAlchemy.*")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _PROJECT_ROOT / "output" / "dashboard" / "cb_dashboard.html"

# ── 指标定义 ──────────────────────────────────────────────
METRICS: List[dict] = [
    {
        "id": "cond_mean",
        "name": "条件价格中枢",
        "table": "cb_daily_mean_price_cond",
        "col": "mean_value",
        "unit": "",
        "decimals": 2,
        "color": "#7c3aed",  # violet
        "source": "daily_agg",  # 预计算的日度聚合表
    },
    {
        "id": "price",
        "name": "全样本价格中枢",
        "table": "cb_daily_median_price",
        "col": "median_value",
        "unit": "元",
        "decimals": 2,
        "color": "#2563eb",  # blue
        "source": "daily_agg",
    },
    {
        "id": "premium",
        "name": "纯债溢价率中枢",
        "table": "cb_daily_median_premium_rate",
        "col": "median_value",
        "unit": "%",
        "decimals": 2,
        "color": "#dc2626",  # red
        "source": "daily_agg",
    },
    {
        "id": "ytm",
        "name": "纯债 YTM 中枢",
        "table": "cb_daily_median_ytm",
        "col": "median_value",
        "unit": "%",
        "decimals": 2,
        "color": "#16a34a",  # green
        "source": "daily_agg",
    },
    {
        "id": "conv_premium",
        "name": "百元溢价率中枢",
        "table": "cb_daily_premium_valuation",
        "col": "100",
        "unit": "%",
        "decimals": 2,
        "color": "#f59e0b",  # amber
        "source": "daily_agg",
        "value_scale": 100.0,  # 表内存储为小数，×100 转为 %
    },
]

HIST_PCT_WINDOWS: Tuple[Tuple[str, str], ...] = (
    ("2017-01-01", "pct_2017"),
    ("2022-01-01", "pct_2022"),
)


# ── 数据库 ────────────────────────────────────────────────
def mysql_connect(
    host: str, port: int, user: str, password: str, database: str
) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=host, port=port, user=user, password=password,
        database=database, charset="utf8mb4",
    )


def sql_col_ident(name: str) -> str:
    """列名为纯数字时需反引号包裹（如 `100`），否则 MySQL 当成数字字面量。"""
    return f"`{name}`" if name.isdigit() else name


def load_series(conn, table: str, col: str) -> pd.DataFrame:
    col_quoted = sql_col_ident(col)
    sql = f"""
    SELECT trade_date, {col_quoted} AS value
    FROM {table}
    WHERE {col_quoted} IS NOT NULL
    ORDER BY trade_date
    """
    df = pd.read_sql(sql, conn)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df.dropna(subset=["value"], inplace=True)
    df.sort_values("trade_date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def load_panel_median(conn, table: str, col: str) -> pd.DataFrame:
    """从面板表 (trade_date, bond_code, value) 计算日度中位数。"""
    sql = f"""
    SELECT trade_date, {col} AS value
    FROM {table}
    WHERE {col} IS NOT NULL
    ORDER BY trade_date
    """
    df = pd.read_sql(sql, conn)
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df.dropna(subset=["value"], inplace=True)
    daily = df.groupby("trade_date")["value"].median().reset_index()
    daily.rename(columns={"value": "value"}, inplace=True)
    daily.sort_values("trade_date", inplace=True)
    daily.reset_index(drop=True, inplace=True)
    return daily


def add_expanding_quantiles(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    s = out["value"]
    out["q25"] = s.expanding(min_periods=1).quantile(0.25)
    out["q50"] = s.expanding(min_periods=1).quantile(0.50)
    out["q75"] = s.expanding(min_periods=1).quantile(0.75)
    return out


def add_historical_percentile_ranks(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    dates = out["trade_date"]
    values = out["value"].to_numpy(dtype=float)
    n = len(out)
    for start_str, col_name in HIST_PCT_WINDOWS:
        start = pd.Timestamp(start_str)
        ranks = np.full(n, np.nan, dtype=float)
        for i in range(n):
            if dates.iloc[i] < start:
                continue
            mask = (dates >= start) & (dates <= dates.iloc[i])
            window = values[mask]
            if window.size == 0:
                continue
            ranks[i] = float(np.mean(window <= values[i])) * 100.0
        out[col_name] = ranks
    return out


def build_metric_data(conn, m: dict) -> dict:
    """加载一个指标的完整时序 + 分位数 + 分位排名，转为 JSON 友好格式。"""
    source = m.get("source", "daily_agg")
    if source == "panel_median":
        df = load_panel_median(conn, m["table"], m["col"])
    else:
        df = load_series(conn, m["table"], m["col"])
    # 值缩放（如 cb_daily_premium_valuation.100 需 ×100 转百分比）
    scale = float(m.get("value_scale", 1.0))
    if df.empty:
        return {"id": m["id"], "name": m["name"], "series": [], "latest": None}
    if scale != 1.0:
        df["value"] = df["value"] * scale
    df = add_expanding_quantiles(df)
    df = add_historical_percentile_ranks(df)

    series = []
    for _, row in df.iterrows():
        pt = {
            "date": row["trade_date"].strftime("%Y-%m-%d"),
            "value": round(float(row["value"]), m["decimals"]),
            "q25": round(float(row["q25"]), m["decimals"]),
            "q50": round(float(row["q50"]), m["decimals"]),
            "q75": round(float(row["q75"]), m["decimals"]),
        }
        for _, cn in HIST_PCT_WINDOWS:
            v = row.get(cn)
            pt[cn] = round(float(v), 2) if not (v is None or (isinstance(v, float) and np.isnan(v))) else None
        series.append(pt)

    last = series[-1] if series else None
    prev = series[-2] if len(series) >= 2 else None
    change = None
    if last and prev and prev["value"] != 0:
        change = round(float((last["value"] - prev["value"]) / abs(prev["value"]) * 100), 2)

    latest_info = None
    if last:
        latest_info = {
            "date": last["date"],
            "value": last["value"],
            "q25": last["q25"],
            "q50": last["q50"],
            "q75": last["q75"],
            "pct_2017": last.get("pct_2017"),
            "pct_2022": last.get("pct_2022"),
            "change": change,
        }

    return {
        "id": m["id"],
        "name": m["name"],
        "unit": m["unit"],
        "decimals": m["decimals"],
        "color": m["color"],
        "series": series,
        "latest": latest_info,
    }


# ── HTML 模板 ─────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>可转债数据跟踪看板</title>
___ECHARTS_INLINE___
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  background: #f1f5f9; color: #1e293b; min-height: 100vh;
}
.header {
  background: #fff; border-bottom: 1px solid #e2e8f0;
  padding: 16px 32px; display: flex; justify-content: space-between; align-items: center;
}
.header h1 { font-size: 20px; font-weight: 700; color: #0f172a; }
.header .meta { font-size: 13px; color: #64748b; }
.header .meta span { margin-left: 16px; }
.container { max-width: 1400px; margin: 0 auto; padding: 20px 24px; }

/* KPI 卡片 */
.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
.kpi-card {
  background: #fff; border-radius: 10px; padding: 18px 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06); border-left: 4px solid #e2e8f0;
  cursor: pointer; transition: box-shadow .2s, transform .2s;
}
.kpi-card:hover { box-shadow: 0 4px 16px rgba(0,0,0,.1); transform: translateY(-1px); }
.kpi-card.active { box-shadow: 0 0 0 2px var(--kpi-color); border-left-color: var(--kpi-color); }
.kpi-card .kpi-label { font-size: 12px; color: #64748b; margin-bottom: 6px; letter-spacing: .5px; }
.kpi-card .kpi-value { font-size: 28px; font-weight: 700; color: #0f172a; }
.kpi-card .kpi-value .unit { font-size: 14px; font-weight: 400; color: #94a3b8; margin-left: 4px; }
.kpi-card .kpi-sub { display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: #64748b; }
.kpi-card .kpi-sub .pct { font-weight: 600; }
.kpi-card .kpi-change { margin-top: 4px; font-size: 13px; font-weight: 600; }
.kpi-card .kpi-change.up { color: #dc2626; }
.kpi-card .kpi-change.down { color: #16a34a; }
.kpi-card .kpi-change.flat { color: #94a3b8; }

/* 主内容 */
.main-layout { display: flex; flex-direction: column; gap: 16px; }
.chart-panel {
  background: #fff; border-radius: 10px; padding: 16px 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.chart-panel .chart-title { font-size: 15px; font-weight: 600; margin-bottom: 8px; color: #334155; }
#main-chart { width: 100%; height: 420px; }
#pct-chart { width: 100%; height: 200px; }

.bottom-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.table-panel {
  background: #fff; border-radius: 10px; padding: 16px 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06); max-height: 360px; overflow: auto;
}
.table-panel h3 { font-size: 14px; font-weight: 600; margin-bottom: 10px; color: #334155; }
.table-panel table { width: 100%; border-collapse: collapse; font-size: 13px; }
.table-panel th {
  position: sticky; top: 0; background: #f8fafc; text-align: right;
  padding: 8px 10px; border-bottom: 2px solid #e2e8f0; color: #64748b; font-weight: 600;
}
.table-panel th:first-child { text-align: left; }
.table-panel td {
  text-align: right; padding: 6px 10px; border-bottom: 1px solid #f1f5f9;
  font-variant-numeric: tabular-nums; font-family: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
}
.table-panel td:first-child { text-align: left; font-family: inherit; }
.table-panel tr:hover td { background: #f8fafc; }

.range-panel {
  background: #fff; border-radius: 10px; padding: 16px 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
}
.range-panel h3 { font-size: 14px; font-weight: 600; margin-bottom: 10px; color: #334155; }
.footer { text-align: center; padding: 12px; font-size: 11px; color: #94a3b8; }

/* 响应式 */
@media (max-width: 900px) {
  .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  .bottom-grid { grid-template-columns: 1fr; }
  .header { flex-direction: column; align-items: flex-start; gap: 8px; }
}

/* 手机端 */
@media (max-width: 640px) {
  .header { padding: 12px 16px; }
  .header h1 { font-size: 17px; }
  .header .meta { font-size: 11px; display: flex; flex-wrap: wrap; gap: 4px 12px; }
  .header .meta span { margin-left: 0; }
  .container { padding: 10px 12px; }
  .kpi-grid { grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .kpi-card { padding: 12px 14px; border-left-width: 3px; }
  .kpi-card .kpi-label { font-size: 11px; }
  .kpi-card .kpi-value { font-size: 22px; }
  .kpi-card .kpi-sub { font-size: 10px; gap: 8px; flex-wrap: wrap; }
  .kpi-card .kpi-change { font-size: 11px; }
  #main-chart { height: 280px; }
  #pct-chart { height: 160px; }
  .chart-panel { padding: 10px 12px; }
  .chart-panel .chart-title { font-size: 13px; }
  .range-panel { padding: 10px 12px; }
  .table-panel { padding: 10px 12px; max-height: 280px; }
  .table-panel table { font-size: 11px; }
  .table-panel th, .table-panel td { padding: 4px 6px; }
  .footer { font-size: 10px; padding: 8px; }
}
</style>
</head>
<body>

<div class="header">
  <h1>📊 可转债数据跟踪看板</h1>
  <div class="meta">
    <span>📅 数据更新至：<strong id="meta-latest-date">—</strong></span>
    <span>🕐 看板生成：<strong>___GENERATED_AT___</strong></span>
    <span>📈 数据起点：<strong>2017-01-01</strong></span>
  </div>
</div>

<div class="container">

  <!-- KPI 卡片 -->
  <div class="kpi-grid" id="kpi-grid"></div>

  <!-- 主图 -->
  <div class="chart-panel">
    <div class="chart-title" id="chart-title">条件价格中枢</div>
    <div id="main-chart"></div>
  </div>

  <!-- 分位排名图 -->
  <div class="chart-panel">
    <div class="chart-title">📈 历史分位排名（pct_2017 / pct_2022）</div>
    <div id="pct-chart"></div>
  </div>

  <!-- 底部：快捷区间 + 数据表 -->
  <div class="bottom-grid">
    <div class="range-panel" id="signals-panel">
      <h3>⚡ 信号预警</h3>
      <div id="signals-content">
        <p style="color:#94a3b8;font-size:12px;">加载中...</p>
      </div>
      <p style="margin-top:10px;font-size:10px;color:#94a3b8;">
        💡 图表支持鼠标框选缩放、滚轮缩放、拖拽平移
      </p>
    </div>
    <div class="table-panel" id="table-panel">
      <h3>📋 最近交易日明细</h3>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>日期</th><th>数值</th><th>q25</th><th>q50</th><th>q75</th><th>2017分位</th><th>2022分位</th>
            </tr>
          </thead>
          <tbody id="data-table-body"></tbody>
        </table>
      </div>
    </div>
  </div>

</div>

<div class="footer">可转债基础数据库 · 点击 KPI 卡片切换指标 · 图表支持滚轮缩放与拖拽</div>

<script>
// ── 数据注入 ─────────────────────────────────────────────
const METRICS_DATA = ___METRICS_JSON___;

const METRICS = [
  { id: "cond_mean", name: "条件价格中枢", unit: "", decimals: 2, color: "#7c3aed" },
  { id: "price", name: "全样本价格中枢", unit: "元", decimals: 2, color: "#2563eb" },
  { id: "conv_premium", name: "百元溢价率中枢", unit: "%", decimals: 2, color: "#f59e0b" },
  { id: "premium", name: "纯债溢价率中枢", unit: "%", decimals: 2, color: "#dc2626" },
  { id: "ytm", name: "纯债 YTM 中枢", unit: "%", decimals: 2, color: "#16a34a" },
];

// ── 状态 ─────────────────────────────────────────────────
let currentMetricId = "cond_mean";

// ── 初始化 ───────────────────────────────────────────────
const mainChart = echarts.init(document.getElementById("main-chart"));
const pctChart = echarts.init(document.getElementById("pct-chart"));

function getMetricData(id) {
  return METRICS_DATA[id] || null;
}

function getCurrentData() {
  return getMetricData(currentMetricId);
}

// ── KPI 卡片渲染 ─────────────────────────────────────────
function renderKPIs() {
  const grid = document.getElementById("kpi-grid");
  let html = "";
  METRICS.forEach(m => {
    const data = getMetricData(m.id);
    if (!data || !data.latest) return;
    const l = data.latest;
    const changeClass = l.change === null ? "flat" : (l.change > 0 ? "up" : (l.change < 0 ? "down" : "flat"));
    const changeArrow = l.change === null ? "—" : (l.change > 0 ? "▲" : (l.change < 0 ? "▼" : "—"));
    const changeStr = l.change !== null ? `${changeArrow} ${Math.abs(l.change).toFixed(2)}%` : "— 持平";
    const activeClass = m.id === currentMetricId ? " active" : "";
    html += `
      <div class="kpi-card${activeClass}" style="--kpi-color:${m.color}" data-metric-id="${m.id}">
        <div class="kpi-label">${m.name}</div>
        <div class="kpi-value">${l.value.toFixed(m.decimals)}<span class="unit">${m.unit}</span></div>
        <div class="kpi-sub">
          <span>分位(2017): <span class="pct">${l.pct_2017 !== null ? l.pct_2017.toFixed(1) + "%" : "—"}</span></span>
          <span>分位(2022): <span class="pct">${l.pct_2022 !== null ? l.pct_2022.toFixed(1) + "%" : "—"}</span></span>
        </div>
        <div class="kpi-change ${changeClass}">${changeStr}<span style="font-weight:400;color:#94a3b8;margin-left:6px;">日环比</span></div>
      </div>`;
  });
  grid.innerHTML = html;
}

// ── 主图渲染 ─────────────────────────────────────────────
function renderMainChart() {
  const data = getCurrentData();
  if (!data || !data.series.length) return;

  const dates = data.series.map(s => s.date);
  const values = data.series.map(s => s.value);
  const q25 = data.series.map(s => s.q25);
  const q50 = data.series.map(s => s.q50);
  const q75 = data.series.map(s => s.q75);

  const metricMeta = METRICS.find(m => m.id === currentMetricId) || METRICS[0];
  document.getElementById("chart-title").textContent = "📈 " + metricMeta.name + " 时序与扩展分位数";

  const option = {
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", crossStyle: { color: "#94a3b8" } },
      valueFormatter: v => v != null ? v.toFixed(metricMeta.decimals) + metricMeta.unit : "—",
    },
    legend: {
      data: ["中枢值", "历史 q25", "历史 q50", "历史 q75"],
      bottom: 0, textStyle: { fontSize: 12 },
    },
    grid: { left: 60, right: 40, top: 20, bottom: 40 },
    xAxis: {
      type: "category", data: dates, boundaryGap: false,
      axisLabel: { formatter: v => v.slice(0, 7), fontSize: 11 },
      axisLine: { lineStyle: { color: "#cbd5e1" } },
    },
    yAxis: {
      type: "value", name: metricMeta.unit,
      scale: true,
      axisLabel: { formatter: v => v.toFixed(metricMeta.decimals) },
      splitLine: { lineStyle: { color: "#f1f5f9" } },
    },
    dataZoom: [
      {
        type: "slider", bottom: 28, height: 20,
        start: 0, end: 100,
        textStyle: { fontSize: 10 },
      },
      {
        type: "inside", start: 0, end: 100,
        zoomOnMouseWheel: true, moveOnMouseMove: true,
      },
    ],
    series: [
      {
        name: "中枢值", type: "line", data: values,
        lineStyle: { color: metricMeta.color, width: 2.2 },
        itemStyle: { color: metricMeta.color },
        symbol: "none", z: 3,
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: metricMeta.color, type: "dashed", width: 1, opacity: 0.6 },
          label: { formatter: "最新\\n{c}", fontSize: 11 },
          data: [{ yAxis: values[values.length - 1] }],
        },
      },
      {
        name: "历史 q25", type: "line", data: q25,
        lineStyle: { color: "#94a3b8", width: 1, type: "dashed" },
        symbol: "none", z: 1,
      },
      {
        name: "历史 q50", type: "line", data: q50,
        lineStyle: { color: "#64748b", width: 1, type: "dashed" },
        symbol: "none", z: 1,
      },
      {
        name: "历史 q75", type: "line", data: q75,
        lineStyle: { color: "#475569", width: 1, type: "dashed" },
        symbol: "none", z: 1,
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(148,163,184,0.18)" },
            { offset: 1, color: "rgba(148,163,184,0.02)" },
          ]),
          origin: "start",
        },
      },
    ],
  };
  mainChart.setOption(option, true);
}

// ── 分位排名图渲染 ───────────────────────────────────────
function renderPctChart() {
  const data = getCurrentData();
  if (!data || !data.series.length) return;

  const dates = data.series.map(s => s.date);
  const pct2017 = data.series.map(s => s.pct_2017);
  const pct2022 = data.series.map(s => s.pct_2022);

  const option = {
    tooltip: {
      trigger: "axis",
      valueFormatter: v => v != null ? v.toFixed(1) + "%" : "—",
    },
    legend: {
      data: ["2017 至今分位", "2022 至今分位"],
      bottom: 0, textStyle: { fontSize: 12 },
    },
    grid: { left: 55, right: 30, top: 12, bottom: 34 },
    xAxis: {
      type: "category", data: dates, boundaryGap: false,
      axisLabel: { formatter: v => v.slice(0, 7), fontSize: 11 },
      axisLine: { lineStyle: { color: "#cbd5e1" } },
    },
    yAxis: {
      type: "value", name: "%", min: 0, max: 100,
      splitLine: { lineStyle: { color: "#f1f5f9" } },
      axisLabel: { formatter: v => v + "%" },
    },
    dataZoom: [
      { type: "slider", bottom: 28, height: 18, textStyle: { fontSize: 10 } },
      { type: "inside", zoomOnMouseWheel: false, moveOnMouseMove: true },
    ],
    series: [
      {
        name: "2017 至今分位", type: "line", data: pct2017,
        lineStyle: { color: "#2563eb", width: 1.6 },
        itemStyle: { color: "#2563eb" },
        symbol: "none",
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: "#2563eb", type: "dashed", width: 1, opacity: 0.5 },
          label: { formatter: "最新\\n{c}%", fontSize: 11 },
          data: [{ yAxis: pct2017[pct2017.length - 1] }],
        },
      },
      {
        name: "2022 至今分位", type: "line", data: pct2022,
        lineStyle: { color: "#dc2626", width: 1.6 },
        itemStyle: { color: "#dc2626" },
        symbol: "none",
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: "#dc2626", type: "dashed", width: 1, opacity: 0.5 },
          label: { formatter: "最新\\n{c}%", fontSize: 11 },
          data: [{ yAxis: pct2022[pct2022.length - 1] }],
        },
      },
    ],
  };
  pctChart.setOption(option, true);
}

// ── 数据表格渲染 ─────────────────────────────────────────
function renderTable() {
  const data = getCurrentData();
  if (!data || !data.series.length) return;
  const rows = data.series.slice(-15).reverse();
  let html = "";
  rows.forEach(r => {
    html += `<tr>
      <td>${r.date}</td>
      <td>${r.value.toFixed(data.decimals)}${data.unit}</td>
      <td>${r.q25.toFixed(data.decimals)}</td>
      <td>${r.q50.toFixed(data.decimals)}</td>
      <td>${r.q75.toFixed(data.decimals)}</td>
      <td>${r.pct_2017 != null ? r.pct_2017.toFixed(1) + "%" : "—"}</td>
      <td>${r.pct_2022 != null ? r.pct_2022.toFixed(1) + "%" : "—"}</td>
    </tr>`;
  });
  document.getElementById("data-table-body").innerHTML = html;
}

// ── 切换指标 ─────────────────────────────────────────────
function switchMetric(id) {
  currentMetricId = id;
  renderKPIs();
  renderMainChart();
  renderPctChart();
  renderTable();
  renderSignals();
  updateMetaDate();
}

// ── 信号预警 ─────────────────────────────────────────────
function computeSignals() {
  const data = getCurrentData();
  if (!data || !data.series.length) return [];
  const s = data.series;
  const latest = s[s.length - 1];
  const signals = [];
  const metricMeta = METRICS.find(m => m.id === currentMetricId) || METRICS[0];

  // 1) 历史分位预警
  if (latest.pct_2017 != null) {
    if (latest.pct_2017 >= 80) signals.push({level:"danger", msg:`2017年以来分位 ${latest.pct_2017.toFixed(0)}%，处于高位`});
    else if (latest.pct_2017 >= 60) signals.push({level:"warn", msg:`2017年以来分位 ${latest.pct_2017.toFixed(0)}%，偏高水平`});
    else if (latest.pct_2017 <= 20) signals.push({level:"info", msg:`2017年以来分位 ${latest.pct_2017.toFixed(0)}%，处于低位`});
    else if (latest.pct_2017 <= 40) signals.push({level:"ok", msg:`2017年以来分位 ${latest.pct_2017.toFixed(0)}%，偏低水平`});
    else signals.push({level:"ok", msg:`2017年以来分位 ${latest.pct_2017.toFixed(0)}%，正常区间`});
  }
  if (latest.pct_2022 != null) {
    if (latest.pct_2022 >= 80) signals.push({level:"warn", msg:`2022年以来分位 ${latest.pct_2022.toFixed(0)}%，近期偏高`});
    else if (latest.pct_2022 <= 20) signals.push({level:"info", msg:`2022年以来分位 ${latest.pct_2022.toFixed(0)}%，近期偏低`});
  }

  // 2) 连续涨跌预警（最近 5 个交易日）
  if (s.length >= 5) {
    const recent = s.slice(-5);
    let upDays = 0, downDays = 0;
    for (let i = 1; i < recent.length; i++) {
      if (recent[i].value > recent[i-1].value) upDays++;
      else if (recent[i].value < recent[i-1].value) downDays++;
    }
    if (upDays >= 4) signals.push({level:"warn", msg:`连续 ${upDays} 日上升，注意趋势`});
    if (downDays >= 4) signals.push({level:"info", msg:`连续 ${downDays} 日下降`});
  }

  // 3) 突破历史 q75/q25 预警
  if (latest.value > latest.q75) signals.push({level:"warn", msg:`当前值 ${latest.value.toFixed(data.decimals)} 突破历史 q75（${latest.q75.toFixed(data.decimals)}）`});
  if (latest.value < latest.q25) signals.push({level:"info", msg:`当前值 ${latest.value.toFixed(data.decimals)} 跌破历史 q25（${latest.q25.toFixed(data.decimals)}）`});

  return signals;
}

function renderSignals() {
  const signals = computeSignals();
  const container = document.getElementById("signals-content");
  if (!signals.length) {
    container.innerHTML = '<p style="color:#94a3b8;font-size:12px;">暂无预警信号</p>';
    return;
  }
  const icons = { danger: "\u{1F534}", warn: "\u{1F7E1}", info: "\u{1F535}", ok: "\u{1F7E2}" };
  const colors = { danger: "#dc2626", warn: "#d97706", info: "#2563eb", ok: "#16a34a" };
  const bgs = { danger: "#fef2f2", warn: "#fffbeb", info: "#eff6ff", ok: "#f0fdf4" };
  let html = "";
  signals.forEach(sig => {
    html += `<div style="display:flex;align-items:flex-start;gap:8px;padding:8px 10px;margin-bottom:6px;background:${bgs[sig.level]};border-radius:6px;border-left:3px solid ${colors[sig.level]};font-size:12px;">
      <span style="font-size:14px;flex-shrink:0;">${icons[sig.level]}</span>
      <span style="color:#334155;line-height:1.5;">${sig.msg}</span>
    </div>`;
  });
  container.innerHTML = html;
}

// ── 元信息 ───────────────────────────────────────────────
function updateMetaDate() {
  const data = getCurrentData();
  if (data && data.series.length) {
    document.getElementById("meta-latest-date").textContent = data.series[data.series.length - 1].date;
  }
}

// ── 响应式 ───────────────────────────────────────────────
window.addEventListener("resize", () => {
  mainChart.resize();
  pctChart.resize();
});

// ── KPI 卡片点击（事件委托）─────────────────────────────
document.getElementById("kpi-grid").addEventListener("click", e => {
  const card = e.target.closest(".kpi-card");
  if (!card) return;
  const mid = card.dataset.metricId;
  if (mid && mid !== currentMetricId) switchMetric(mid);
});

// ── 启动 ─────────────────────────────────────────────────
renderKPIs();
renderMainChart();
renderPctChart();
renderTable();
renderSignals();
updateMetaDate();
// 如果 2 秒后 signals 还是"加载中"，说明 JS 未执行
setTimeout(() => {
  const sc = document.getElementById("signals-content");
  if (sc && sc.textContent.includes("加载中")) {
    sc.innerHTML = '<p style="color:#dc2626;font-size:12px;">JS 加载异常，请尝试刷新页面或检查网络（ECharts CDN）</p>';
  }
}, 2000);
</script>
</body>
</html>"""


# ── ECharts 内嵌（离线可用）─────────────────────────────────
_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"
_ECHARTS_CACHE = _PROJECT_ROOT / "output" / "dashboard" / ".echarts_cache.js"


def _get_echarts_js() -> str:
    """获取 ECharts JS 内容，优先本地缓存，否则从 CDN 下载并缓存。"""
    import hashlib
    import urllib.request

    # 1) 本地缓存命中
    if _ECHARTS_CACHE.exists():
        cached = _ECHARTS_CACHE.read_text(encoding="utf-8")
        if len(cached) > 100_000:  # 健全检查
            return cached

    # 2) 从 CDN 下载
    try:
        req = urllib.request.Request(_ECHARTS_CDN, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            js = resp.read().decode("utf-8")
        if len(js) > 100_000:
            _ECHARTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _ECHARTS_CACHE.write_text(js, encoding="utf-8")
            return js
    except Exception:
        pass

    return ""  # 下载失败，后续 fallback 到 CDN


# ── HTML 生成 ─────────────────────────────────────────────
def build_html(all_data: dict, generated_at: str) -> str:
    # 当前使用 CDN 加载 ECharts（稳定可靠）
    # 如需离线版本，可在有网时生成一次后，手动下载 echarts.min.js 放到 output/dashboard/ 目录
    inline = f'<script src="{_ECHARTS_CDN}"></script>'
    # 如果本地有离线缓存也尝试加载（回退）
    if _ECHARTS_CACHE.exists():
        inline += f'\n<script>if(typeof echarts==="undefined"){{document.write(\'<script src="echarts.min.js"><\\/script>\')}}</script>'

    return HTML_TEMPLATE.replace(
        "___ECHARTS_INLINE___", inline
    ).replace(
        "___GENERATED_AT___", generated_at
    ).replace(
        "___METRICS_JSON___", json.dumps(all_data, ensure_ascii=False, indent=2)
    )


# ── 主入口 ────────────────────────────────────────────────
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="生成可转债数据跟踪 HTML 看板")
    parser.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "161106"))
    parser.add_argument("--db", dest="database", default=os.getenv("MYSQL_DB", "cb_data"))
    parser.add_argument(
        "--output", type=Path, default=_DEFAULT_OUTPUT,
        help=f"输出 HTML 路径，默认 {_DEFAULT_OUTPUT}",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    print(f"MySQL: {args.user}@{args.host}:{args.port}/{args.database}")
    conn = mysql_connect(args.host, args.port, args.user, args.password, args.database)

    all_data = {}
    try:
        for m in METRICS:
            print(f"  Loading: {m['name']} ... ", end="", flush=True)
            all_data[m["id"]] = build_metric_data(conn, m)
            n = len(all_data[m["id"]]["series"])
            latest = all_data[m["id"]]["latest"]
            if latest:
                print(f"{n} rows, latest={latest['date']} value={latest['value']}")
            else:
                print("empty")
    finally:
        conn.close()

    # 检查是否所有指标都为空
    if all(len(d["series"]) == 0 for d in all_data.values()):
        print("\n[WARN] 所有指标表均为空，请先运行 Wind 同步管线。")
        print("  python run_daily_pipeline.py --write")
        return 1

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = build_html(all_data, generated_at)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"\n[OK] 看板已生成: {args.output}")
    print(f"   文件大小: {len(html.encode('utf-8')) / 1024:.0f} KB")
    print(f"   浏览器打开: file:///{args.output.as_posix()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

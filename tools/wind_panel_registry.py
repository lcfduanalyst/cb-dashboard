# -*- coding: utf-8 -*-
"""
可转债面板表 ↔ Wind 字段注册表。

新增基础数据表时，只需在本文件追加映射，并执行对应建表 SQL。
"""

from __future__ import annotations

from typing import Dict, Literal, Sequence, TypedDict


class PanelFieldSpec(TypedDict):
    wind_field: str
    value_type: Literal["numeric", "text"]


# 原有 7 张核心面板（数值）
CORE_NUMERIC_PANEL_TABLES: Dict[str, str] = {
    "cb_panel_price": "close",
    "cb_panel_scale": "outstandingbalance",
    "cb_panel_conv_premium_rate": "convpremiumratio",
    "cb_panel_turnover_rate": "turn",
    "cb_panel_pure_bond_premium_rate": "strbpremiumratio",
    "cb_panel_bond_floor": "strbvalue",
    "cb_panel_pure_bond_ytm": "ytm_cb",
}

# 扩展基础数据（数值）——按价格表键对齐，每日 wss + 历史补数
EXTRA_NUMERIC_PANEL_TABLES: Dict[str, str] = {
    "cb_panel_pct_chg": "pct_chg",
    "cb_panel_remaining_term": "ptmyear",
    "cb_panel_implied_vol": "impliedvol",
}

# 扩展基础数据（文本）
TEXT_PANEL_TABLES: Dict[str, str] = {
    "cb_panel_rating": "latestissurercreditrating",
}

# 需用 w.wsd 拉取的表（字段名以 Wind 终端实测为准：latestissurercreditrating）
WSD_PANEL_TABLES: Dict[str, str] = {
    "cb_panel_rating": "latestissurercreditrating",
}

# w.wss 额外 option（不含 tradeDate；与 bulk 多字段不兼容时需单独拉取）
WSS_EXTRA_OPTIONS: Dict[str, str] = {
    "cb_panel_implied_vol": "rfIndex=1",
}

# 每日同步/补洞时排除的转债（如上市取消），统一大写 Wind 代码
EXCLUDED_BOND_CODES = frozenset({"123095.SZ"})


def numeric_panel_tables() -> Dict[str, str]:
    return {**CORE_NUMERIC_PANEL_TABLES, **EXTRA_NUMERIC_PANEL_TABLES}


def extra_panel_tables() -> Dict[str, str]:
    """仅扩展表（不含原 7 张核心表）。"""
    return {**EXTRA_NUMERIC_PANEL_TABLES, **TEXT_PANEL_TABLES}


def all_panel_tables() -> Dict[str, str]:
    return {**numeric_panel_tables(), **TEXT_PANEL_TABLES}


def fill_panel_tables() -> Dict[str, str]:
    """以 cb_panel_price 为键补洞时可处理的表（不含 price 本身）。"""
    return {k: v for k, v in all_panel_tables().items() if k != "cb_panel_price"}


def panel_value_type(table: str) -> Literal["numeric", "text"]:
    if table in TEXT_PANEL_TABLES:
        return "text"
    return "numeric"


def panel_value_column(table: str) -> str:
    return "value_text" if panel_value_type(table) == "text" else "value"


def panel_wind_api(table: str) -> Literal["wss", "wsd"]:
    if table in WSD_PANEL_TABLES:
        return "wsd"
    return "wss"


def panel_wss_extra_options(table: str) -> str:
    """w.wss 在 tradeDate 之后追加的 option，如 impliedvol 需 rfIndex=1。"""
    return WSS_EXTRA_OPTIONS.get(table, "")


def is_excluded_bond_code(code: object) -> bool:
    if code is None:
        return False
    s = str(code).strip().upper()
    return bool(s) and s in EXCLUDED_BOND_CODES


def filter_excluded_bond_codes(codes: Sequence[str]) -> list[str]:
    """去掉上市取消等黑名单券。"""
    out: list[str] = []
    seen: set[str] = set()
    for c in codes:
        if not c or is_excluded_bond_code(c):
            continue
        sc = str(c).strip().upper()
        if sc not in seen:
            seen.add(sc)
            out.append(sc)
    return sorted(out)

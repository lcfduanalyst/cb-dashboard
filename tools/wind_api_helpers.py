# -*- coding: utf-8 -*-
"""Wind wsd 拉数辅助：评级等文本字段（单日、多券）。"""

from __future__ import annotations

import math
import re
import sys
import time
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence

DEFAULT_WSD_BATCH_SIZE = 200

_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$", re.IGNORECASE)


def _is_missing(x: object) -> bool:
    if x is None:
        return True
    if isinstance(x, float) and math.isnan(x):
        return True
    return False


def _iter_chunks(items: Sequence[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield list(items[i : i + n])


def normalize_bond_code_for_wind(code: object) -> Optional[str]:
    """规范为 XXXXXX.SH/SZ；6 位数字按 11→SH、12→SZ。"""
    if code is None:
        return None
    s = str(code).strip().upper()
    if not s:
        return None
    if _CODE_RE.match(s):
        return s
    if len(s) == 6 and s.isdigit():
        if s.startswith("11"):
            return f"{s}.SH"
        if s.startswith("12"):
            return f"{s}.SZ"
    return None


def _scalar_from_wsd_cell(cell: object) -> Optional[object]:
    """单日 wsd：cell 为标量，或仅取序列第一个有效值。"""
    if _is_missing(cell):
        return None
    if isinstance(cell, (list, tuple)):
        for x in cell:
            if not _is_missing(x):
                return x
        return None
    return cell


def _per_code_series(r_data: list, n_codes: int) -> list:
    """
    取各券当日序列。Wind 常见：
    - Data = [ [券1序列], [券2序列], ... ]  → 用 Data[0] 层（单字段）
    - 少数返回 Data 直接按券展开
    """
    if not r_data:
        return []
    if len(r_data) == 1 and isinstance(r_data[0], (list, tuple)):
        inner = r_data[0]
        if inner and len(inner) == n_codes:
            return list(inner)
    if len(r_data) == n_codes:
        return list(r_data)
    if len(r_data) == 1:
        return list(r_data[0]) if isinstance(r_data[0], (list, tuple)) else [r_data[0]]
    return list(r_data[0]) if r_data else []


def _wsd_fetch_chunk(
    w,
    codes: Sequence[str],
    wind_field: str,
    trade_date: date,
    pause_ms: int,
) -> Dict[str, str]:
    """单日 w.wsd → bond_code -> 评级文本。"""
    d_str = trade_date.strftime("%Y-%m-%d")
    fname = wind_field.strip().lower()

    wind_codes = [c for c in (normalize_bond_code_for_wind(x) for x in codes) if c]
    if not wind_codes:
        return {}

    r = w.wsd(",".join(wind_codes), wind_field, d_str, d_str, "")
    if getattr(r, "ErrorCode", -1) != 0:
        raise RuntimeError(
            f"Wind wsd 失败：date={d_str} field={wind_field} "
            f"ErrorCode={r.ErrorCode} Data={getattr(r, 'Data', None)}"
        )

    r_codes = list(getattr(r, "Codes", []) or []) or wind_codes
    r_data = getattr(r, "Data", None)
    if not r_data or not isinstance(r_data, list):
        print(f"    wsd empty Data: {d_str} codes={len(wind_codes)}", file=sys.stderr)
        time.sleep(pause_ms / 1000.0)
        return {}

    series_list = _per_code_series(r_data, len(r_codes))
    out: Dict[str, str] = {}

    for i, code in enumerate(r_codes):
        if i >= len(series_list):
            break
        v = _scalar_from_wsd_cell(series_list[i])
        if _is_missing(v):
            continue
        sv = str(v).strip()
        if not sv or sv.lower() in ("nan", "none"):
            continue
        out[str(code).strip().upper()] = sv

    if not out:
        print(
            f"    wsd parsed 0: {d_str} codes={len(wind_codes)} data_len={len(r_data)}",
            file=sys.stderr,
        )

    time.sleep(pause_ms / 1000.0)
    return out


def wind_fetch_wsd_text_by_date(
    w,
    wind_field: str,
    trade_date: date,
    bond_codes: Sequence[str],
    batch_size: int,
    pause_ms: int,
    log=None,  # 保留参数兼容；日志已内置 stderr
) -> Dict[str, Dict[str, str]]:
    """
    拉取单日文本字段（如 latestissurercreditrating）。

    专用口径：w.wsd(codes, field, "YYYY-MM-DD", "YYYY-MM-DD", "")
    """
    _ = log
    fname = wind_field.strip().lower()
    merged: Dict[str, Dict[str, str]] = {}
    batch = max(1, batch_size)

    for chunk in _iter_chunks(list(bond_codes), batch):
        try:
            part = _wsd_fetch_chunk(w, chunk, wind_field, trade_date, pause_ms)
        except RuntimeError:
            part = {}
            for code in chunk:
                try:
                    part.update(_wsd_fetch_chunk(w, [code], wind_field, trade_date, pause_ms))
                except RuntimeError as e:
                    print(f"    wsd failed {code} {trade_date}: {e}", file=sys.stderr)

        for bc, val in part.items():
            merged.setdefault(bc, {})[fname] = val

    return merged

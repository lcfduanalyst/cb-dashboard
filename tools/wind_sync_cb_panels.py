#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按 Wind 交易日历，在日期区间内逐日同步可转债面板数据到 MySQL。

流程（每个交易日）：
1) w.tdays 得到区间内交易日列表
2) w.wset 取当日板块/指数成分（转债代码池），option 由 --wind-set-template 提供，需含占位符 {wind_date}
3) w.wss 一次拉多字段（按 tradeDate），拆分到各 cb_panel_* 表并 UPSERT

与 wind_fill_by_price.py 的区别：本脚本按板块全量拉取并写入；
补洞脚本只补“价格表已有但某指标为 NULL”的键。

写库（--write）完成后，默认自动执行 sql/更新_衍生指标.sql（转股价值、日度中位数、条件价格等）；
可用 --skip-derived 跳过。


终端运行（不写库）：python tools/wind_sync_cb_panels.py --start-date 2026-01-01 --end-date 2026-01-10 --wind-set-template "date={wind_date};sectorid=你的板块ID" --dry-run
终端运行（写库）：python tools/wind_sync_cb_panels.py --start-date 2026-01-01 --end-date 2026-01-10 --wind-set-template "date={wind_date};sectorid=你的板块ID" --db cb_data --user root --password "你的密码" --write

"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

import pymysql

from wind_api_helpers import DEFAULT_WSD_BATCH_SIZE, wind_fetch_wsd_text_by_date
from wind_panel_registry import (
    all_panel_tables,
    panel_value_type,
    panel_wind_api,
    panel_wss_extra_options,
    filter_excluded_bond_codes,
)

# ----------------------------
# 可手工修改的默认配置（直接点绿色运行也能跑）
# ----------------------------
DEFAULT_START_DATE = "2026-07-1"
DEFAULT_END_DATE = "2026-07-2"
DEFAULT_WIND_SET_TEMPLATE = "date={wind_date};sectorid=1000073208000000"
DEFAULT_DRY_RUN = True  # 默认只演练不写库；需要写库请传 --write
DEFAULT_DERIVED_SQL = _REPO_ROOT / "sql" / "更新_衍生指标.sql"

# 表 ↔ Wind 字段见 wind_panel_registry.py（新增表只需改注册表 + 建表 SQL）


@dataclass(frozen=True)
class MysqlCfg:
    host: str
    port: int
    user: str
    password: str
    database: str


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _to_wind_trade_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def _is_missing(x: object) -> bool:
    if x is None:
        return True
    if isinstance(x, float) and math.isnan(x):
        return True
    return False


def _iter_chunks(items: Sequence[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield list(items[i : i + n])


def mysql_connect(cfg: MysqlCfg) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.Cursor,
    )


def upsert_panel_rows_numeric(
    conn: pymysql.connections.Connection,
    table: str,
    rows: Sequence[Tuple[date, str, float]],
) -> int:
    if not rows:
        return 0
    sql = f"""
    INSERT INTO {table} (trade_date, bond_code, value)
    VALUES (%s, %s, %s)
    ON DUPLICATE KEY UPDATE value = VALUES(value)
    """
    params = [(r[0].strftime("%Y-%m-%d"), r[1], r[2]) for r in rows]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
        return cur.rowcount


def upsert_panel_rows_text(
    conn: pymysql.connections.Connection,
    table: str,
    rows: Sequence[Tuple[date, str, str]],
) -> int:
    if not rows:
        return 0
    sql = f"""
    INSERT INTO {table} (trade_date, bond_code, value_text)
    VALUES (%s, %s, %s)
    ON DUPLICATE KEY UPDATE value_text = VALUES(value_text)
    """
    params = [(r[0].strftime("%Y-%m-%d"), r[1], r[2]) for r in rows]
    with conn.cursor() as cur:
        cur.executemany(sql, params)
        return cur.rowcount


def wind_connect():
    try:
        from WindPy import w  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "无法导入 WindPy。请确认已安装 Wind 终端且当前 Python 可 import WindPy。"
        ) from e
    if not w.isconnected():
        r = w.start()
        if getattr(r, "ErrorCode", -1) != 0:
            raise RuntimeError(f"WindPy 启动失败：{getattr(r, 'Data', r)}")
    return w


def wind_trade_dates(w, start: date, end: date) -> List[date]:
    """区间内 Wind 交易日列表（升序）。"""
    s = start.strftime("%Y-%m-%d")
    e = end.strftime("%Y-%m-%d")
    r = w.tdays(s, e, "")
    if getattr(r, "ErrorCode", -1) != 0:
        raise RuntimeError(f"Wind tdays 失败：{s}..{e} ErrorCode={r.ErrorCode} Data={r.Data}")
    data = getattr(r, "Data", None)
    if not data or not isinstance(data, list) or not data[0]:
        return []
    out: List[date] = []
    for x in data[0]:
        if isinstance(x, datetime):
            out.append(x.date())
        elif isinstance(x, date):
            out.append(x)
        else:
            try:
                out.append(_parse_date(str(x)[:10]))
            except Exception:
                continue
    return sorted(set(out))


_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ)$", re.IGNORECASE)


def _normalize_wind_code(x: object) -> Optional[str]:
    if x is None or (isinstance(x, float) and math.isnan(float(x))):
        return None
    s = str(x).strip().upper()
    if not s:
        return None
    if _CODE_RE.match(s):
        return s
    return None


def wind_sector_codes(w, set_type: str, option: str) -> List[str]:
    """
    w.wset(set_type, option) 解析出 Wind 证券代码列表。
    option 示例：date=20170103;sectorid=xxxx 或 date=20170103;windcode=881001.WI
    """
    r = w.wset(set_type, option)
    if getattr(r, "ErrorCode", -1) != 0:
        raise RuntimeError(f"Wind wset 失败：type={set_type} option={option} ErrorCode={r.ErrorCode}")

    codes_attr = getattr(r, "Codes", None)
    if codes_attr:
        raw = list(codes_attr) if not isinstance(codes_attr, str) else [codes_attr]
        out = [_normalize_wind_code(c) for c in raw]
        codes = sorted({c for c in out if c})
        # 有些 wset 返回的 Codes 是 1,2,3... 的行号；此时需要从 Data 的 wind_code 列取真正证券代码
        if codes:
            return codes

    data = getattr(r, "Data", None)
    if not data:
        return []

    # 兼容：Data 为“按字段分列”的结构，优先用 Fields 找到 wind_code 列
    fields_attr = getattr(r, "Fields", None)
    col0 = None
    if fields_attr and isinstance(fields_attr, (list, tuple)):
        flds = [str(f).strip().lower() for f in list(fields_attr)]
        # 常见字段名：wind_code / windcode / code / sec_code
        for key in ("wind_code", "windcode", "sec_code", "code"):
            if key in flds:
                idx = flds.index(key)
                if isinstance(data, list) and idx < len(data):
                    col0 = data[idx]
                break

    # 如果找不到 wind_code 列，则回退到 Data[0]
    if col0 is None:
        col0 = data[0] if isinstance(data[0], (list, tuple)) else data
    if not isinstance(col0, (list, tuple)):
        col0 = [col0]

    out: List[str] = []
    for cell in col0:
        c = _normalize_wind_code(cell)
        if c:
            out.append(c)
    return sorted(set(out))


def wind_wss_multi(
    w,
    codes: Sequence[str],
    wind_fields: str,
    trade_date: date,
    batch_size: int,
    pause_ms: int,
    zero_as_null: bool,
    text_field_names: Optional[Sequence[str]] = None,
    wss_option_suffix: str = "",
) -> Dict[str, Dict[str, Union[float, str]]]:
    """
    多字段 wss，返回：bond_code -> {wind_field_lower: float|str}
    仅包含有值的字段键。text_field_names 中的字段按文本保留（如评级）。
    wss_option_suffix：tradeDate 后追加的 option，如 rfIndex=1（impliedvol 必需）。
    """
    text_fields = {f.strip().lower() for f in (text_field_names or []) if f.strip()}
    td_opt = _to_wind_trade_date(trade_date)
    opt = f"tradeDate={td_opt}"
    extra = wss_option_suffix.strip()
    if extra:
        opt = f"{opt};{extra}"
    merged: Dict[str, Dict[str, Union[float, str]]] = {}

    for chunk in _iter_chunks(list(codes), batch_size):
        codes_arg = ",".join(chunk)
        r = w.wss(codes_arg, wind_fields, opt)
        if getattr(r, "ErrorCode", -1) != 0:
            raise RuntimeError(
                f"Wind wss 失败：tradeDate={td_opt} fields={wind_fields} ErrorCode={r.ErrorCode} Data={r.Data}"
            )

        r_codes = list(getattr(r, "Codes", []) or [])
        r_data = getattr(r, "Data", None)
        if not r_codes or not r_data or not isinstance(r_data, list):
            time.sleep(pause_ms / 1000.0)
            continue

        fields_attr = getattr(r, "Fields", None)
        if fields_attr:
            flds = [str(f).lower() for f in list(fields_attr)]
        else:
            flds = [f.strip().lower() for f in wind_fields.split(",") if f.strip()]

        # WindPy 多字段：Data[i] 为第 i 个字段在各证券上的取值序列，长度应与 Codes 一致
        for fi, fname in enumerate(flds):
            if fi >= len(r_data):
                break
            col = r_data[fi]
            if not isinstance(col, (list, tuple)):
                col = [col]
            for ci, code in enumerate(r_codes):
                if ci >= len(col):
                    break
                v = col[ci]
                if _is_missing(v):
                    continue
                sc = _normalize_wind_code(code)
                if not sc:
                    continue
                if fname in text_fields:
                    sv = str(v).strip()
                    if not sv or sv.lower() in ("nan", "none"):
                        continue
                    merged.setdefault(sc, {})[fname] = sv
                    continue
                try:
                    fv = float(v)
                except Exception:
                    continue
                if zero_as_null and fv == 0.0:
                    continue
                merged.setdefault(sc, {})[fname] = fv

        time.sleep(pause_ms / 1000.0)

    return merged


def wind_filter_codes_by_issue_method(
    w,
    trade_date: date,
    codes: Sequence[str],
    batch_size: int,
    pause_ms: int,
) -> List[str]:
    """
    过滤发行方式为“定向/私募”的代码。

    使用字段：issue_issuemethod
    - 若该字段缺失/空，默认保留（避免误删）。
    - 命中“定向”或“私募”（包含匹配）则剔除。
    """
    keep: List[str] = []

    for chunk in _iter_chunks(list(codes), batch_size):
        codes_arg = ",".join(chunk)
        # issue_issuemethod 属于静态属性字段，通常不需要 tradeDate
        r = w.wss(codes_arg, "issue_issuemethod")
        if getattr(r, "ErrorCode", -1) != 0:
            raise RuntimeError(
                f"Wind wss(issue_issuemethod) 失败：ErrorCode={r.ErrorCode} Data={r.Data}"
            )

        r_codes = list(getattr(r, "Codes", []) or [])
        r_data = getattr(r, "Data", None)
        if not r_codes or not r_data or not isinstance(r_data, list) or not r_data[0]:
            time.sleep(pause_ms / 1000.0)
            continue

        methods = r_data[0]
        for code, m in zip(r_codes, methods):
            sc = _normalize_wind_code(code)
            if not sc:
                continue
            if m is None:
                keep.append(sc)
                continue
            ms = str(m).strip()
            if not ms:
                keep.append(sc)
                continue
            if ("定向" in ms) or ("私募" in ms):
                continue
            keep.append(sc)

        time.sleep(pause_ms / 1000.0)

    return sorted(set(keep))


def _strip_sql_line_comments(text: str) -> str:
    lines: List[str] = []
    for line in text.splitlines():
        if "--" in line:
            line = line[: line.index("--")]
        lines.append(line)
    return "\n".join(lines)


def split_sql_statements(text: str) -> List[str]:
    """按分号拆分 SQL 文件（去掉 -- 行注释）。"""
    cleaned = _strip_sql_line_comments(text)
    return [part.strip() for part in cleaned.split(";") if part.strip()]


def run_sql_file(conn: pymysql.connections.Connection, path: Path) -> int:
    """逐条执行 SQL 文件中的语句，返回执行条数。"""
    if not path.is_file():
        raise FileNotFoundError(f"SQL 文件不存在：{path}")
    statements = split_sql_statements(path.read_text(encoding="utf-8"))
    if not statements:
        raise ValueError(f"SQL 文件无有效语句：{path}")

    with conn.cursor() as cur:
        for i, stmt in enumerate(statements, 1):
            cur.execute(stmt)
            print(f"  [{i}/{len(statements)}] ok (rowcount={cur.rowcount})")
    conn.commit()
    return len(statements)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按 Wind 交易日 + 板块成分，同步可转债面板数据到 MySQL（UPSERT）。"
    )
    parser.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "161106"))
    parser.add_argument("--db", dest="database", default=os.getenv("MYSQL_DB", "cb_data"))
    parser.add_argument(
        "--start-date",
        default=DEFAULT_START_DATE,
        help=f"开始日期 YYYY-MM-DD（含），默认 {DEFAULT_START_DATE}",
    )
    parser.add_argument(
        "--end-date",
        default=DEFAULT_END_DATE,
        help=f"结束日期 YYYY-MM-DD（含），默认 {DEFAULT_END_DATE}",
    )
    parser.add_argument(
        "--wind-set-type",
        default="sectorconstituent",
        help="w.wset 第一个参数，默认 sectorconstituent",
    )
    parser.add_argument(
        "--wind-set-template",
        default=DEFAULT_WIND_SET_TEMPLATE,
        help="w.wset 第二个参数字符串模板，必须包含 {wind_date}（YYYYMMDD），"
        f"例如：date={{wind_date}};sectorid=你的板块ID 或 date={{wind_date}};windcode=881001.WI。默认 {DEFAULT_WIND_SET_TEMPLATE}",
    )
    parser.add_argument(
        "--tables",
        default="all",
        help="要同步的表名，逗号分隔；默认 all（全部注册面板表）。示例：cb_panel_price,cb_panel_rating",
    )
    parser.add_argument(
        "--wind-field-price",
        default=os.getenv("WIND_FIELD_PRICE", "close"),
        help="cb_panel_price 对应的 Wind 字段名（可转债收盘价常用 close，可按终端实际调整）",
    )
    parser.add_argument("--batch-size", type=int, default=200, help="每次 wss 请求的转债数量")
    parser.add_argument("--pause-ms", type=int, default=200, help="Wind 请求间隔（毫秒）")
    parser.add_argument(
        "--zero-as-null",
        action="store_true",
        help="Wind 返回 0 时跳过写入（保持 NULL）",
    )
    write_group = parser.add_mutually_exclusive_group()
    write_group.add_argument(
        "--dry-run",
        action="store_true",
        default=DEFAULT_DRY_RUN,
        help="只打印统计，不写 MySQL（默认开启；要写库请传 --write）",
    )
    write_group.add_argument(
        "--write",
        dest="dry_run",
        action="store_false",
        help="写入 MySQL（关闭 dry-run）",
    )
    parser.add_argument(
        "--skip-derived",
        action="store_true",
        help="写库后跳过衍生指标刷新（默认 --write 时执行 sql/更新_衍生指标.sql）",
    )
    parser.add_argument(
        "--derived-sql",
        default=str(DEFAULT_DERIVED_SQL),
        help=f"衍生指标 SQL 文件路径，默认 {DEFAULT_DERIVED_SQL.name}",
    )
    args = parser.parse_args()

    start_d = _parse_date(args.start_date)
    end_d = _parse_date(args.end_date)
    if start_d > end_d:
        raise SystemExit("start-date 不能晚于 end-date")

    table_to_field = dict(all_panel_tables())
    table_to_field["cb_panel_price"] = args.wind_field_price.strip()

    if args.tables.strip().lower() == "all":
        tables = list(table_to_field.keys())
    else:
        tables = [x.strip() for x in args.tables.split(",") if x.strip()]
        unknown = [t for t in tables if t not in table_to_field]
        if unknown:
            raise SystemExit(f"未知表名：{unknown}。可选：{list(table_to_field.keys())}")

    wss_tables = [t for t in tables if panel_wind_api(t) == "wss"]
    wsd_tables = [t for t in tables if panel_wind_api(t) == "wsd"]
    wss_bulk_tables = [t for t in wss_tables if not panel_wss_extra_options(t)]
    wss_solo_tables = [t for t in wss_tables if panel_wss_extra_options(t)]
    wind_fields = ",".join(dict.fromkeys([table_to_field[t] for t in wss_bulk_tables]))
    # Wind 字段 -> 目标表（小写字段名映射）
    field_lower_to_table: Dict[str, str] = {}
    for t in tables:
        fn = table_to_field[t].lower()
        if fn in field_lower_to_table and field_lower_to_table[fn] != t:
            raise SystemExit(f"Wind 字段冲突：{fn} 对应多张表")
        field_lower_to_table[fn] = t

    cfg = MysqlCfg(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
    )

    print(f"MySQL: {cfg.user}@{cfg.host}:{cfg.port}/{cfg.database}")
    print(f"Tables: {tables}")
    print(f"Wind wss fields (bulk): {wind_fields or '(none)'}")
    if wss_solo_tables:
        solo_desc = [
            f"{t}({table_to_field[t]};{panel_wss_extra_options(t)})" for t in wss_solo_tables
        ]
        print(f"Wind wss solo: {solo_desc}")
    if wsd_tables:
        print(f"Wind wsd tables: {wsd_tables}")

    w = wind_connect()
    trade_days = wind_trade_dates(w, start_d, end_d)
    print(f"Wind trade days in range: {len(trade_days)}")

    conn_ctx = mysql_connect(cfg) if not args.dry_run else None
    try:
        conn = conn_ctx
        total_rows = 0
        for td in trade_days:
            wd = _to_wind_trade_date(td)
            set_opt = args.wind_set_template.format(wind_date=wd)
            try:
                codes = wind_sector_codes(w, args.wind_set_type, set_opt)
            except Exception as e:
                print(f"[{td}] wset failed: {e}", file=sys.stderr)
                continue

            if not codes:
                print(f"[{td}] wset returned 0 codes, skip")
                continue

            # 发行方式过滤：去掉定向/私募
            try:
                codes = wind_filter_codes_by_issue_method(
                    w=w,
                    trade_date=td,
                    codes=codes,
                    batch_size=args.batch_size,
                    pause_ms=args.pause_ms,
                )
            except Exception as e:
                print(f"[{td}] issue_method filter failed: {e}", file=sys.stderr)
                continue

            if not codes:
                print(f"[{td}] all codes filtered by issue method, skip")
                continue

            before_ex = len(codes)
            codes = filter_excluded_bond_codes(codes)
            if before_ex != len(codes):
                print(f"[{td}] excluded bond filter: {before_ex} -> {len(codes)}")

            if not codes:
                print(f"[{td}] all codes excluded by bond blacklist, skip")
                continue

            print(f"[{td}] universe size(after filter)={len(codes)}")
            per_code: Dict[str, Dict[str, Union[float, str]]] = {}
            if wind_fields:
                try:
                    per_code = wind_wss_multi(
                        w=w,
                        codes=codes,
                        wind_fields=wind_fields,
                        trade_date=td,
                        batch_size=args.batch_size,
                        pause_ms=args.pause_ms,
                        zero_as_null=args.zero_as_null,
                        text_field_names=[],
                    )
                except Exception as e:
                    print(f"[{td}] wss failed: {e}", file=sys.stderr)
                    continue

            for solo_tbl in wss_solo_tables:
                solo_field = table_to_field[solo_tbl]
                solo_extra = panel_wss_extra_options(solo_tbl)
                try:
                    solo_part = wind_wss_multi(
                        w=w,
                        codes=codes,
                        wind_fields=solo_field,
                        trade_date=td,
                        batch_size=args.batch_size,
                        pause_ms=args.pause_ms,
                        zero_as_null=args.zero_as_null,
                        text_field_names=[],
                        wss_option_suffix=solo_extra,
                    )
                    for bc, fvmap in solo_part.items():
                        per_code.setdefault(bc, {}).update(fvmap)
                except Exception as e:
                    print(f"[{td}] wss({solo_tbl}) failed: {e}", file=sys.stderr)

            for wsd_tbl in wsd_tables:
                wsd_field = table_to_field[wsd_tbl]
                try:
                    wsd_part = wind_fetch_wsd_text_by_date(
                        w=w,
                        wind_field=wsd_field,
                        trade_date=td,
                        bond_codes=codes,
                        batch_size=args.batch_size or DEFAULT_WSD_BATCH_SIZE,
                        pause_ms=args.pause_ms,
                    )
                    for bc, fvmap in wsd_part.items():
                        per_code.setdefault(bc, {}).update(fvmap)
                except Exception as e:
                    print(f"[{td}] wsd({wsd_tbl}) failed: {e}", file=sys.stderr)

            if args.dry_run:
                sample = sum(len(v) for v in per_code.values())
                print(f"[{td}] dry-run: got field-values for {len(per_code)} bonds, total cells~{sample}")
                continue

            assert conn is not None
            by_numeric: Dict[str, List[Tuple[date, str, float]]] = {
                t: [] for t in tables if panel_value_type(t) == "numeric"
            }
            by_text: Dict[str, List[Tuple[date, str, str]]] = {
                t: [] for t in tables if panel_value_type(t) == "text"
            }
            for bond_code, fvmap in per_code.items():
                for fl, val in fvmap.items():
                    tbl = field_lower_to_table.get(fl)
                    if not tbl or tbl not in tables:
                        continue
                    if panel_value_type(tbl) == "text":
                        by_text[tbl].append((td, bond_code, str(val)))
                    else:
                        by_numeric[tbl].append((td, bond_code, float(val)))

            for tbl, rows in by_numeric.items():
                if not rows:
                    continue
                try:
                    upsert_panel_rows_numeric(conn, tbl, rows)
                    conn.commit()
                    total_rows += len(rows)
                    print(f"[{td}] {tbl}: wrote {len(rows)}")
                except Exception as e:
                    conn.rollback()
                    print(f"[{td}] {tbl} upsert failed: {e}", file=sys.stderr)

            for tbl, rows in by_text.items():
                if not rows:
                    continue
                try:
                    upsert_panel_rows_text(conn, tbl, rows)
                    conn.commit()
                    total_rows += len(rows)
                    print(f"[{td}] {tbl}: wrote {len(rows)}")
                except Exception as e:
                    conn.rollback()
                    print(f"[{td}] {tbl} upsert failed: {e}", file=sys.stderr)

        if not args.dry_run:
            print(f"\nDone. Total row writes (sum across tables): {total_rows}")
            if not args.skip_derived:
                derived_path = Path(args.derived_sql)
                print(f"\nRefreshing derived indicators: {derived_path}")
                try:
                    assert conn is not None
                    n = run_sql_file(conn, derived_path)
                    print(f"Derived indicators done ({n} statements).")
                except Exception as e:
                    conn.rollback()
                    print(f"Derived indicators failed: {e}", file=sys.stderr)
                    raise SystemExit(1) from e
        else:
            print("\nDry-run done.")
    finally:
        if conn_ctx is not None:
            conn_ctx.close()


if __name__ == "__main__":
    main()

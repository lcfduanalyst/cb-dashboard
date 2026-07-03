#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
以 cb_panel_price 的 (trade_date, bond_code) 为基准，补齐其它面板表的缺口。

流程：
1) MySQL 查找缺口：price 表有键，但目标表 value/value_text 为 NULL（或无行）
2) 按交易日分组，Wind wss/wsd 仅拉缺口券
3) UPSERT 回对应表

适用全部注册面板（见 wind_panel_registry.fill_panel_tables），含数值与文本（评级 wsd）。

示例：
  python tools/wind_fill_by_price.py --tables all
  python tools/wind_fill_by_price.py --tables cb_panel_rating,cb_panel_turnover_rate --write
  python tools/wind_fill_by_price.py --tables cb_panel_pct_chg --start-date 2026-01-01 --write
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pymysql

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from wind_api_helpers import (
    DEFAULT_WSD_BATCH_SIZE,
    normalize_bond_code_for_wind,
    wind_fetch_wsd_text_by_date,
)
from wind_panel_registry import (
    fill_panel_tables,
    panel_value_column,
    panel_value_type,
    panel_wind_api,
    panel_wss_extra_options,
    is_excluded_bond_code,
)


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


def fetch_missing_keys(
    conn: pymysql.connections.Connection,
    panel_table: str,
    start_date: Optional[date],
    end_date: Optional[date],
) -> List[Tuple[date, str]]:
    value_col = panel_value_column(panel_table)
    where = []
    params: List[object] = []
    if start_date:
        where.append("p.trade_date >= %s")
        params.append(start_date.strftime("%Y-%m-%d"))
    if end_date:
        where.append("p.trade_date <= %s")
        params.append(end_date.strftime("%Y-%m-%d"))
    where_sql = (" AND " + " AND ".join(where)) if where else ""

    sql = f"""
    SELECT p.trade_date, p.bond_code
    FROM cb_panel_price p
    LEFT JOIN {panel_table} f
      ON f.trade_date = p.trade_date AND f.bond_code = p.bond_code
    WHERE f.{value_col} IS NULL
    {where_sql}
    ORDER BY p.trade_date, p.bond_code
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    out: List[Tuple[date, str]] = []
    for td, bc in rows:
        if isinstance(td, datetime):
            td2 = td.date()
        elif isinstance(td, date):
            td2 = td
        else:
            td2 = _parse_date(str(td))
        out.append((td2, str(bc)))
    return out


def upsert_numeric_rows(
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


def upsert_text_rows(
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


def wind_fetch_wss_by_date(
    w,
    wind_field: str,
    trade_date: date,
    bond_codes: Sequence[str],
    batch_size: int,
    pause_ms: int,
    zero_as_null: bool,
    wss_option_suffix: str = "",
) -> Dict[str, float]:
    td_opt = _to_wind_trade_date(trade_date)
    opt = f"tradeDate={td_opt}"
    extra = wss_option_suffix.strip()
    if extra:
        opt = f"{opt};{extra}"
    out: Dict[str, float] = {}

    for chunk in _iter_chunks(list(bond_codes), batch_size):
        r = w.wss(",".join(chunk), wind_field, opt)
        if getattr(r, "ErrorCode", -1) != 0:
            raise RuntimeError(
                f"Wind wss 失败：field={wind_field} tradeDate={td_opt} "
                f"ErrorCode={r.ErrorCode} Data={r.Data}"
            )

        codes = list(getattr(r, "Codes", []) or [])
        data = getattr(r, "Data", [])
        if not codes or not data or not isinstance(data, list) or not data[0]:
            time.sleep(pause_ms / 1000.0)
            continue

        for code, v in zip(codes, data[0]):
            if _is_missing(v):
                continue
            try:
                fv = float(v)
            except Exception:
                continue
            if zero_as_null and fv == 0.0:
                continue
            out[str(code).strip().upper()] = fv

        time.sleep(pause_ms / 1000.0)

    return out


def main() -> None:
    panel_specs = fill_panel_tables()

    parser = argparse.ArgumentParser(
        description="以 cb_panel_price 为基准，补齐注册面板表的缺失指标（仅拉 NULL 缺口）。"
    )
    parser.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", ""))
    parser.add_argument("--db", dest="database", default=os.getenv("MYSQL_DB", "cb_data"))
    parser.add_argument("--start-date", default=None, help="仅补该日期（含）之后，YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="仅补该日期（含）之前，YYYY-MM-DD")
    parser.add_argument(
        "--tables",
        default="all",
        help="表名逗号分隔；默认 all。可选：" + ",".join(panel_specs.keys()),
    )
    parser.add_argument("--batch-size", type=int, default=200, help="w.wss 每批券数")
    parser.add_argument(
        "--wsd-batch-size",
        type=int,
        default=DEFAULT_WSD_BATCH_SIZE,
        help=f"w.wsd（文本字段）每批券数，默认 {DEFAULT_WSD_BATCH_SIZE}",
    )
    parser.add_argument("--pause-ms", type=int, default=200)
    parser.add_argument("--zero-as-null", action="store_true")
    write_group = parser.add_mutually_exclusive_group()
    write_group.add_argument("--dry-run", action="store_true", default=True)
    write_group.add_argument("--write", dest="dry_run", action="store_false")
    args = parser.parse_args()

    start_d = _parse_date(args.start_date) if args.start_date else None
    end_d = _parse_date(args.end_date) if args.end_date else None

    if args.tables.strip().lower() == "all":
        tables = list(panel_specs.keys())
    else:
        tables = [x.strip() for x in args.tables.split(",") if x.strip()]
        unknown = [t for t in tables if t not in panel_specs]
        if unknown:
            raise SystemExit(f"未知表名：{unknown}。可选：{list(panel_specs.keys())}")

    cfg = MysqlCfg(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
    )

    print(f"MySQL: {cfg.user}@{cfg.host}:{cfg.port}/{cfg.database}")
    print(f"Tables: {tables}")
    if args.dry_run:
        print("Mode: dry-run（仅统计，不写库；写库请加 --write）")

    w = wind_connect()

    with mysql_connect(cfg) as conn:
        total_written = 0
        for panel_table in tables:
            wind_field = panel_specs[panel_table]
            api = panel_wind_api(panel_table)
            print(f"\n==> Missing keys: {panel_table} (Wind: {wind_field}, api={api})")
            missing = fetch_missing_keys(conn, panel_table, start_d, end_d)
            print(f"Missing keys: {len(missing)}")
            if not missing:
                continue

            by_date: Dict[date, List[str]] = {}
            for td, bc in missing:
                if is_excluded_bond_code(bc):
                    continue
                by_date.setdefault(td, []).append(bc)

            for td in sorted(by_date.keys()):
                db_codes = sorted(set(by_date[td]))
                wind_to_db: Dict[str, str] = {}
                wind_codes: List[str] = []
                for bc in db_codes:
                    wc = normalize_bond_code_for_wind(bc)
                    if wc:
                        wind_codes.append(wc)
                        wind_to_db[wc] = bc
                skipped = len(db_codes) - len(wind_codes)
                print(
                    f"  - {td.isoformat()} bonds: {len(db_codes)}"
                    + (f" (skip invalid code: {skipped})" if skipped else "")
                )
                if not wind_codes:
                    print(f"    no valid Wind codes for {td}, skip", file=sys.stderr)
                    continue

                try:
                    if api == "wsd":
                        got = wind_fetch_wsd_text_by_date(
                            w=w,
                            wind_field=wind_field,
                            trade_date=td,
                            bond_codes=wind_codes,
                            batch_size=args.wsd_batch_size,
                            pause_ms=args.pause_ms,
                        )
                        fld = wind_field.lower()
                        rows = [
                            (td, wind_to_db.get(wc, wc), str(fvmap[fld]))
                            for wc, fvmap in got.items()
                            if fld in fvmap
                        ]
                    else:
                        got = wind_fetch_wss_by_date(
                            w=w,
                            wind_field=wind_field,
                            trade_date=td,
                            bond_codes=wind_codes,
                            batch_size=args.batch_size,
                            pause_ms=args.pause_ms,
                            zero_as_null=args.zero_as_null,
                            wss_option_suffix=panel_wss_extra_options(panel_table),
                        )
                        rows = [
                            (td, wind_to_db.get(wc, wc), fv)
                            for wc, fv in got.items()
                        ]
                except Exception as e:
                    print(f"    Wind fetch failed: {e}", file=sys.stderr)
                    continue

                if not rows:
                    print(
                        f"    no rows to write: {td} requested={len(wind_codes)}",
                        file=sys.stderr,
                    )
                    continue

                if args.dry_run:
                    print(f"    dry-run: would write {len(rows)} rows")
                    continue

                try:
                    if panel_value_type(panel_table) == "text":
                        upsert_text_rows(
                            conn, panel_table, [(r[0], r[1], str(r[2])) for r in rows]
                        )
                    else:
                        upsert_numeric_rows(
                            conn, panel_table, [(r[0], r[1], float(r[2])) for r in rows]
                        )
                    conn.commit()
                    total_written += len(rows)
                    print(f"    wrote {len(rows)} rows")
                except Exception as e:
                    conn.rollback()
                    print(f"    MySQL upsert failed: {e}", file=sys.stderr)

        if args.dry_run:
            print("\nDry-run done.")
        else:
            print(f"\nDone. Total rows written: {total_written}")


if __name__ == "__main__":
    main()

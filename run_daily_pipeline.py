#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可转债基础数据库 —— 每日管线总控脚本。

流程（6 步）：
  1. wind_sync_cb_panels        Wind → MySQL 面板数据（含条件价格指数等衍生指标）
  2. run_premium_by_conv_value  截面回归 → 各平价转股溢价率估值表
  3. calc_industry_index        行业指数 → 各行业转债加权指数走势
  4. run_central_quantiles      中枢分位数 → Excel / 图
  5. generate_dashboard         HTML 数据看板
  6. git_push                   自动提交看板并推送到 GitHub Pages

用法：
  python run_daily_pipeline.py --write                     # 正式写入当天
  python run_daily_pipeline.py --date 2026-07-02 --write   # 指定日期
  python run_daily_pipeline.py --dry-run                   # 演练，不写库
  python run_daily_pipeline.py --write --skip-sync         # 跳过 Wind 同步
  python run_daily_pipeline.py --write --skip-premium      # 跳过溢价率回归
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ╔══════════════════════════════════════════════════════════════╗
# ║  PyCharm 快捷运行：直接点绿色箭头即可，改下面的值控制行为     ║
# ╚══════════════════════════════════════════════════════════════╝
PYCHARM_START_DATE = "2026-07-07"       # 开始日期，空=今天（如 "2026-07-03"）
PYCHARM_END_DATE = ""         # 结束日期，空=同开始日期
PYCHARM_WRITE_MODE = True    # True=正式写入MySQL  False=演练不写库
PYCHARM_SKIP_SYNC = False     # True=跳过 Wind 同步
PYCHARM_SKIP_PREMIUM = False  # True=跳过溢价率回归
PYCHARM_SKIP_QUANTILES = False  # True=跳过分位数 Excel
PYCHARM_SKIP_DASHBOARD = False  # True=跳过 HTML 看板
PYCHARM_SKIP_PUSH = False       # True=跳过 Git 推送
PYCHARM_SKIP_INDUSTRY = False   # True=跳过行业指数
# ══════════════════════════════════════════════════════════════

# ── 项目路径 ─────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
TOOLS_DIR = PROJECT_ROOT / "tools"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = OUTPUT_DIR / "logs"

DEFAULT_WIND_SET_TEMPLATE = "date={wind_date};sectorid=1000073208000000"
TOTAL_STEPS = 6


# ── Python 解释器 ─────────────────────────────────────────
def _find_python() -> str:
    return sys.executable


# ── 日志 ──────────────────────────────────────────────────
def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"pipeline_{ts}.log"

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(fh)
    root.addHandler(ch)

    return log_path


# ── 辅助 ──────────────────────────────────────────────────
def _run_subprocess(cmd: list[str], step_label: str, cwd: Path | None = None) -> bool:
    """运行子进程，stdout/stderr 实时输出。返回 True=成功。"""
    logging.info(f"[{step_label}] 执行: {' '.join(cmd)}")
    start = time.time()
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd or PROJECT_ROOT), capture_output=False, text=True,
        )
    except FileNotFoundError as e:
        logging.error(f"[{step_label}] 命令未找到: {e}")
        return False
    elapsed = time.time() - start
    ok = result.returncode == 0
    if ok:
        logging.info(f"[{step_label}] 完成 ({elapsed:.0f}s)")
    else:
        logging.error(f"[{step_label}] 失败 (exit={result.returncode}, {elapsed:.0f}s)")
    return ok


# ── Wind 检查 ─────────────────────────────────────────────
def check_wind() -> bool:
    try:
        from WindPy import w
    except Exception:
        logging.error("无法导入 WindPy，请确认 Wind 终端已安装")
        return False
    if not w.isconnected():
        try:
            r = w.start()
            if getattr(r, "ErrorCode", -1) != 0:
                logging.error(f"WindPy 启动失败: {getattr(r, 'Data', r)}")
                return False
        except Exception as e:
            logging.error(f"WindPy 连接异常: {e}")
            return False
    logging.info("WindPy 连接正常")
    return True


# ── Step 1: Wind 同步 ─────────────────────────────────────
def step_sync(args: argparse.Namespace) -> bool:
    python = _find_python()
    cmd = [
        python,
        str(TOOLS_DIR / "wind_sync_cb_panels.py"),
        "--start-date", args.start_date,
        "--end-date", args.end_date,
        "--wind-set-template", args.wind_set_template,
        "--host", args.host, "--port", str(args.port),
        "--user", args.user, "--password", args.password,
        "--db", args.database,
        "--batch-size", str(args.batch_size),
        "--pause-ms", str(args.pause_ms),
    ]
    if args.zero_as_null:
        cmd.append("--zero-as-null")
    if args.tables != "all":
        cmd.extend(["--tables", args.tables])
    cmd.append("--write" if not args.dry_run else "--dry-run")
    return _run_subprocess(cmd, f"Step 1/{TOTAL_STEPS}: Wind同步")


# ── Step 2: 百元溢价率回归 ────────────────────────────────
def step_premium_valuation(args: argparse.Namespace) -> bool:
    """对数指数拟合：截面回归 → cb_daily_premium_valuation（列 60~150）。"""
    python = _find_python()
    cmd = [
        python,
        str(TOOLS_DIR / "run_premium_by_conv_value.py"),
        "--start-date", args.start_date,
        "--end-date", args.end_date,
        "--host", args.host, "--port", str(args.port),
        "--user", args.user, "--password", args.password,
        "--db", args.database,
    ]
    if args.dry_run:
        logging.info(f"[Step 2/{TOTAL_STEPS}: 溢价率回归] 演练模式，跳过实际执行")
        return True
    return _run_subprocess(cmd, f"Step 2/{TOTAL_STEPS}: 溢价率回归")


# ── Step 3: 行业指数 ────────────────────────────────────
def step_industry_index(args: argparse.Namespace) -> bool:
    """计算各行业转债加权指数，写入 cb_strategy.cb_industry_index。"""
    python = _find_python()
    cmd = [
        python,
        str(TOOLS_DIR / "calc_industry_index.py"),
        "--host", args.host, "--port", str(args.port),
        "--user", args.user, "--password", args.password,
        "--lookback", "30",  # 每日只重算最近 30 天
    ]
    if args.dry_run:
        logging.info(f"[Step 3/{TOTAL_STEPS}: 行业指数] 演练模式，跳过实际执行")
        return True
    cmd.append("--write")
    return _run_subprocess(cmd, f"Step 3/{TOTAL_STEPS}: 行业指数")


# ── Step 4: 中枢分位数 Excel ──────────────────────────────
def step_quantiles(args: argparse.Namespace) -> bool:
    python = _find_python()
    cmd = [
        python,
        str(TOOLS_DIR / "run_central_quantiles.py"),
        "--host", args.host, "--port", str(args.port),
        "--user", args.user, "--password", args.password,
        "--db", args.database,
        "--output-dir", str(args.output_dir / "central_quantiles"),
        "--write-excel",
    ]
    if args.plot:
        cmd.append("--plot")
    if args.also_fixed_name:
        cmd.append("--also-fixed-name")
    return _run_subprocess(cmd, f"Step 4/{TOTAL_STEPS}: 分位数Excel")


# ── Step 5: HTML 看板 ─────────────────────────────────────
def step_dashboard(args: argparse.Namespace) -> bool:
    python = _find_python()
    cmd = [
        python,
        str(TOOLS_DIR / "generate_dashboard.py"),
        "--host", args.host, "--port", str(args.port),
        "--user", args.user, "--password", args.password,
        "--db", args.database,
        "--output", str(args.output_dir / "dashboard" / "cb_dashboard.html"),
    ]
    return _run_subprocess(cmd, f"Step 5/{TOTAL_STEPS}: HTML看板")


# ── Step 6: Git 推送（自动更新 GitHub Pages）──────────────
def step_git_push(args: argparse.Namespace) -> bool:
    """将看板 HTML 提交并推送到 GitHub，自动更新 GitHub Pages 链接。"""
    label = f"Step 6/{TOTAL_STEPS}: Git推送"
    if args.dry_run:
        logging.info(f"[{label}] 演练模式，跳过")
        return True

    import subprocess as sp

    def _git(cmd: list[str]) -> tuple[int, str, str]:
        """运行 git 命令，返回 (returncode, stdout, stderr)。"""
        r = sp.run(
            ["git"] + cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True, text=True,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()

    # 1) 暂存看板文件
    dashboard_path = str(args.output_dir / "dashboard" / "cb_dashboard.html")
    rc, out, err = _git(["add", dashboard_path])
    if rc != 0:
        logging.error(f"[{label}] git add 失败: {err}")
        return False

    # 检查是否有改动
    rc, out, err = _git(["diff", "--cached", "--quiet"])
    if rc == 0:
        logging.info(f"[{label}] 看板无变化，跳过推送")
        return True  # 没有改动不算失败

    # 2) 提交
    today = date.today().strftime("%Y-%m-%d")
    commit_msg = f"auto: dashboard update {today}"
    rc, out, err = _git(["commit", "-m", commit_msg])
    if rc != 0:
        logging.error(f"[{label}] git commit 失败: {err}")
        return False
    logging.info(f"[{label}] 提交: {commit_msg}")

    # 3) 推送
    rc, out, err = _git(["push"])
    if rc != 0:
        logging.error(f"[{label}] git push 失败（网络问题？）: {err}")
        logging.warning(f"[{label}] 可稍后手动执行: git push")
        return False
    logging.info(f"[{label}] 已推送到 GitHub → https://lcfduanalyst.github.io/cb-dashboard/")
    return True


# ── 参数解析 ──────────────────────────────────────────────
def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    today_str = date.today().strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="可转债每日管线一键运行（6 步）")

    # 日期 — 所有依赖日期的步骤统一使用
    parser.add_argument("--date", default=None,
                        help=f"单个日期 YYYY-MM-DD，默认今天 ({today_str})")
    parser.add_argument("--start-date", default=None,
                        help="开始日期 YYYY-MM-DD，不指定则跟随 --date")
    parser.add_argument("--end-date", default=None,
                        help="结束日期 YYYY-MM-DD，不指定则跟随 --date")

    # MySQL
    parser.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    parser.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", "161106"))
    parser.add_argument("--db", dest="database", default=os.getenv("MYSQL_DB", "cb_data"))

    # Wind
    parser.add_argument("--wind-set-template", default=DEFAULT_WIND_SET_TEMPLATE)
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--pause-ms", type=int, default=200)
    parser.add_argument("--zero-as-null", action="store_true")
    parser.add_argument("--tables", default="all")

    # 模式
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="演练不写库（默认），--write 正式写入")
    parser.add_argument("--write", dest="dry_run", action="store_false",
                        help="正式写入 MySQL")

    # 输出
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--plot", action="store_true", help="同时生成 PNG 图")
    parser.add_argument("--also-fixed-name", action="store_true",
                        help="分位数 Excel 同时出固定文件名快照")

    # 跳过步骤
    parser.add_argument("--skip-sync", action="store_true", help="跳过 Wind 同步")
    parser.add_argument("--skip-premium", action="store_true", help="跳过溢价率回归")
    parser.add_argument("--skip-industry", action="store_true", help="跳过行业指数")
    parser.add_argument("--skip-quantiles", action="store_true", help="跳过分位数 Excel")
    parser.add_argument("--skip-dashboard", action="store_true", help="跳过 HTML 看板")
    parser.add_argument("--skip-push", action="store_true", help="跳过 Git 推送")

    args = parser.parse_args(argv)

    # ── PyCharm 绿色箭头运行时，顶部配置覆盖默认值 ─────
    is_pycharm_run = (argv is None and len(sys.argv) <= 1)
    if is_pycharm_run:
        if PYCHARM_START_DATE:
            args.date = PYCHARM_START_DATE
        if PYCHARM_WRITE_MODE:
            args.dry_run = False
        if PYCHARM_SKIP_SYNC:
            args.skip_sync = True
        if PYCHARM_SKIP_PREMIUM:
            args.skip_premium = True
        if PYCHARM_SKIP_INDUSTRY:
            args.skip_industry = True
        if PYCHARM_SKIP_QUANTILES:
            args.skip_quantiles = True
        if PYCHARM_SKIP_DASHBOARD:
            args.skip_dashboard = True
        if PYCHARM_SKIP_PUSH:
            args.skip_push = True

    ref = args.date or today_str
    if args.start_date is None:
        args.start_date = ref
    if args.end_date is None:
        args.end_date = ref
    if is_pycharm_run and PYCHARM_END_DATE:
        args.end_date = PYCHARM_END_DATE
    return args


# ── 主入口 ────────────────────────────────────────────────
def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    log_path = setup_logging(LOG_DIR)

    mode = "DRY-RUN" if args.dry_run else "WRITE"

    # 收集步骤（仅用于日志展示）
    steps = []
    if not args.skip_sync: steps.append("sync")
    if not args.skip_premium: steps.append("premium")
    if not args.skip_industry: steps.append("industry")
    if not args.skip_quantiles: steps.append("quantiles")
    if not args.skip_dashboard: steps.append("dashboard")
    if not args.skip_push: steps.append("push")

    logging.info("=" * 55)
    logging.info(
        f"可转债每日管线  模式={mode}  日期={args.start_date}~{args.end_date}"
    )
    logging.info(f"MySQL: {args.user}@{args.host}:{args.port}/{args.database}")
    logging.info(f"步骤: {' >> '.join(steps)}")
    logging.info(f"日志: {log_path}")
    logging.info("=" * 55)

    # Wind 检查（仅 sync 需要）
    if not args.skip_sync:
        if not check_wind():
            logging.critical("WindPy 不可用，请先登录 Wind 终端")
            return 1

    results: dict[str, bool] = {}
    abort_on_failure = not args.dry_run

    # Step 1
    if not args.skip_sync:
        results["sync"] = step_sync(args)
        if not results["sync"] and abort_on_failure:
            logging.critical("Wind 同步失败，管线中止（可用 --skip-sync 跳过）")
            return 1
    else:
        logging.info(f"[Step 1/{TOTAL_STEPS}: Wind同步] 已跳过")

    # Step 2
    if not args.skip_premium:
        results["premium"] = step_premium_valuation(args)
        if not results["premium"] and abort_on_failure:
            logging.warning("溢价率回归失败，继续后续步骤")
    else:
        logging.info(f"[Step 2/{TOTAL_STEPS}: 溢价率回归] 已跳过")

    # Step 3
    if not args.skip_industry:
        results["industry"] = step_industry_index(args)
    else:
        logging.info(f"[Step 3/{TOTAL_STEPS}: 行业指数] 已跳过")

    # Step 4
    if not args.skip_quantiles:
        results["quantiles"] = step_quantiles(args)
    else:
        logging.info(f"[Step 4/{TOTAL_STEPS}: 分位数Excel] 已跳过")

    # Step 5
    if not args.skip_dashboard:
        results["dashboard"] = step_dashboard(args)
    else:
        logging.info(f"[Step 5/{TOTAL_STEPS}: HTML看板] 已跳过")

    # Step 6
    if not args.skip_push:
        results["push"] = step_git_push(args)
    else:
        logging.info(f"[Step 6/{TOTAL_STEPS}: Git推送] 已跳过")

    # 汇总
    logging.info("=" * 55)
    if args.dry_run:
        logging.info("管线 DRY-RUN 完成（未写入数据库）")
    else:
        failed = [k for k, v in results.items() if not v]
        if failed:
            logging.warning(f"管线完成，以下步骤失败: {failed}")
        else:
            logging.info("管线全部完成")
    logging.info(f"日志: {log_path}")
    logging.info("=" * 55)

    ok = all(results.values()) if results else True
    return 0 if (ok or args.dry_run) else 1


if __name__ == "__main__":
    sys.exit(main())

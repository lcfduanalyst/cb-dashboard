## 目标

可转债面板数据存入 MySQL 8.0（Navicat 管理），由 WindPy 脚本拉数并 UPSERT 更新：

- 缺失值保持 **NULL**（Wind 无数据或 `--zero-as-null` 时不写入）
- **可每日更新**（主键 `(trade_date, bond_code)`，同日同券覆盖）
- 新上市转债自动插入新 `bond_code`，无需改表结构

依赖：`pip install -r requirements.txt`；Wind 数据需本机 Wind 终端可 `import WindPy`。

---

## 一、MySQL 建表（Navicat 执行 SQL）

1) 在 Navicat 里新建/选择库（建议字符集 `utf8mb4`）

2) 执行 `.\sql\schema_cb_panels_mysql8.sql`（7 张核心面板表）

3) 执行 `.\sql\schema_cb_panels_extra_mysql8.sql`（扩展表）

| 表名 | 含义 | Wind 字段 | 值列 |
|------|------|-----------|------|
| `cb_panel_rating` | 主体信用评级 | `latestissurercreditrating`（**wsd**，拼写为 issurer） | `value_text` |
| `cb_panel_pct_chg` | 日涨跌幅 | `pct_chg` | `value` |
| `cb_panel_remaining_term` | 剩余期限(年) | `ptmyear` | `value` |
| `cb_panel_implied_vol` | 隐含波动率 | `impliedvol`（wss 需 `rfIndex=1`） | `value` |

债券余额沿用 `cb_panel_scale`（`outstandingbalance`）。

**新增指标**：在 `tools/wind_panel_registry.py` 追加映射，并补充建表 SQL。

**排除上市取消等转债**：在 `EXCLUDED_BOND_CODES` 中维护（当前含 `123095.SZ`）；同步/补洞脚本会自动跳过。删除历史并重算衍生表见 `sql/delete_bond_123095_and_refresh_derived.sql`。

---

## 二、每日更新（推荐）

脚本：`tools/wind_sync_cb_panels.py`

按 Wind 交易日 + 板块成分，逐日 `w.wset` 取券池 → `w.wss`/`w.wsd` 拉字段 → UPSERT 到注册表。

```bash
python tools/wind_sync_cb_panels.py --start-date 2026-06-08 --end-date 2026-06-08 ^
  --wind-set-template "date={wind_date};sectorid=1000073208000000" ^
  --db cb_data --write
```

`--wind-set-template` 须含 `{wind_date}`（替换为 `YYYYMMDD`）。板块 ID / 指数代码请在 Wind 终端验证。

MySQL 连接：`--host/--port/--user/--password/--db`，或环境变量 `MYSQL_*`。

---

## 三、按 price 键补洞（省 Wind 请求）

脚本：`tools/wind_fill_by_price.py`

以 `cb_panel_price` 为基准，只补其它表中 **value/value_text 为 NULL** 的缺口。

```bash
# 演练（默认 dry-run）
python tools/wind_fill_by_price.py --db cb_data

# 正式写入
python tools/wind_fill_by_price.py --db cb_data --write

# 仅补部分表
python tools/wind_fill_by_price.py --tables cb_panel_rating,cb_panel_turnover_rate --start-date 2026-03-01 --write
```

可补表见 `wind_panel_registry.fill_panel_tables()`（除 price 外全部注册表）。

---

## 四、两个 Wind 脚本的分工

| 脚本 | 适用 |
|------|------|
| **`wind_sync_cb_panels.py`** | 每日全量刷新；新指标按日期区间初始化 |
| **`wind_fill_by_price.py`** | 漏数修补；新指标历史回填（只拉 NULL，更省请求） |

**推荐组合**：每日 `wind_sync_cb_panels.py --write`；有漏数再用 `wind_fill_by_price.py --write`。

---

## 五、wind_sync 详细说明

### Wind 成分参数

```text
date={wind_date};sectorid=你的可转债板块ID
```

或：

```text
date={wind_date};windcode=881001.WI
```

`--wind-set-type` 默认 `sectorconstituent`。

### 价格字段

`cb_panel_price` 默认 Wind 字段 `close`，可用 `--wind-field-price` 或 `WIND_FIELD_PRICE` 覆盖。

### 运行示例

**演练（不写库）**：

```bash
python tools/wind_sync_cb_panels.py --start-date 2026-01-01 --end-date 2026-01-10 ^
  --wind-set-template "date={wind_date};sectorid=你的ID" ^
  --db cb_data --dry-run
```

**正式写入**：

```bash
python tools/wind_sync_cb_panels.py --start-date 2026-01-01 --end-date 2026-01-10 ^
  --wind-set-template "date={wind_date};sectorid=你的ID" ^
  --user root --password "你的密码" --db cb_data --write
```

可选：`--tables cb_panel_price,cb_panel_turnover_rate` 只同步部分表；`--zero-as-null`；`--batch-size` / `--pause-ms` 控制限频。

Wind 字段试跑：`python tools/test.py`

---

## 六、条件价格指数（筛选后价格样本均值）

表名：`cb_daily_mean_price_cond`（`trade_date` 主键，`mean_value`）

**剔除规则**（满足任一条即不参与）：

1. 价格 > 130 且 转股溢价率 > 50%
2. 价格 > 120 且 转股溢价率 > 75%
3. 价格 > 150
4. 规模 < 3 **亿元**（`cb_panel_scale.value`）
5. **无评级**，或评级低于 A+（保留 AAA / AA+ / AA / AA- / A+）

### 建表（一次）

```text
sql/schema_cb_conditional_price_index_mysql8.sql
```

若曾建旧表 `cb_daily_median_price_cond`，建表 SQL 内含 `DROP` 说明。

### 首次灌历史（全量）

```text
sql/refresh_conditional_price_index_full.sql
```

### 每日更新（Wind 同步 + 其它衍生 SQL 之后执行）

```text
sql/refresh_conditional_price_index.sql
```

默认重算最近 **30** 个交易日（可在 SQL 里改 `@recent_days`）。

### 核对

```sql
SELECT trade_date, mean_value
FROM cb_daily_mean_price_cond
ORDER BY trade_date DESC
LIMIT 10;
```

---

## 七、中枢 + 扩展窗口分位数 → Excel / 图

脚本：`tools/run_central_quantiles.py`（全量重算，只读 MySQL）

| 指标 | MySQL 表 | 值列 |
|------|----------|------|
| 纯债溢价率中枢 | `cb_daily_median_premium_rate` | `median_value` |
| 全样本价格中枢 | `cb_daily_median_price` | `median_value` |
| 纯债 YTM 中枢 | `cb_daily_median_ytm` | `median_value` |
| 条件价格中枢 | `cb_daily_mean_price_cond` | `mean_value` |
| 百元转股溢价率 | `cb_daily_premium_valuation` | 列 `100`（表内小数，输出为 %） |

每个交易日输出：

| 列 | 含义 |
|----|------|
| `trade_date, value` | 日期与中枢值 |
| `q25, q50, q75` | 截至当日的历史扩展分位数（阈值） |
| `pct_2017` | 当日 value 在 2017-01-01 以来历史中的分位（0~100） |
| `pct_2022` | 当日 value 在 2022-01-01 以来历史中的分位（0~100） |

```powershell
pip install -r requirements.txt

$env:MYSQL_PASSWORD = "你的密码"
$env:MYSQL_DB = "cb_data"

# 写 Excel（默认 output/central_quantiles/central_quantiles_YYYYMMDD.xlsx）
python tools/run_central_quantiles.py --write-excel

# 同时出图
python tools/run_central_quantiles.py --write-excel --plot

# 固定文件名快照
python tools/run_central_quantiles.py --write-excel --also-fixed-name
```

建议顺序：Wind 同步 → Navicat 衍生 SQL + 条件价格 refresh → `run_premium_by_conv_value.py` → 本脚本。

---

## 八、百元溢价率估值（对数指数拟合）

表名：`cb_daily_premium_valuation`（列结构同历史 `溢价率估值.xlsx`）

| 列 | 说明 |
|----|------|
| `trade_date` | 交易日（主键） |
| `c`, `a`, `b` | 回归系数（b 即 beta） |
| `60` … `150` | 各转股价值档位溢价率（**小数**，如 0.25 表示 25%） |

Navicat 先执行 `sql/schema_cb_daily_premium_valuation_mysql8.sql` 建表；历史数据若已导入可跳过。

脚本：`tools/run_premium_by_conv_value.py`（单文件：读面板 → 回归 → UPSERT MySQL）

| 表 | 用途 |
|----|------|
| `cb_panel_price` | 收盘价 |
| `cb_panel_conv_value` | 转股价值 |
| `cb_panel_conv_premium_rate` | 转股溢价率（筛选） |
| `cb_panel_turnover_rate` | 换手率 |
| `cb_panel_scale` | 规模（亿元） |

PyCharm 直接运行：修改脚本内 `DEFAULT_START_DATE`、`DEFAULT_END_DATE`。

```powershell
python tools/run_premium_by_conv_value.py
python tools/run_premium_by_conv_value.py --start-date 2026-06-24 --end-date 2026-06-26
```

同一日期重复运行会 **覆盖** 该行（ON DUPLICATE KEY UPDATE）。

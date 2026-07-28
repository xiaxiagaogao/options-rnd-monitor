# 币安开仓点锚定到 RND 图 — 设计

> 日期：2026-07-28
> 状态：设计定稿，待实现
> 相关：`holdings-sync-framework.md`（持仓同步 SSOT）、`rnd-dashboard-spec.md` §8（仪表盘）

## 1. 目标

持仓同步已让 RND 池自动跟随币安持仓。本特性再进一步：把每个持仓标的的**当前持仓开仓日**从 `fund.db` 派生出来，锚定到该标的的 RND 视图上，让用户一眼看到「我建仓那天的风险中性密度长什么样、到现在怎么演变」。

复用仪表盘已有的两处机制，不新造轮子：
- 密度图已有的「vs 入场」对照模式（`cmpMode==="entry"`）
- 扇形/分位时序图已有的 markLine 标记数组（现画 roll / gate_fail / bimodal 三种标记）

**非目标**：不自动创建 `trade_journal` 条目。journal 是「有意识下注 + 止损理由」的手动纪律层，框架特意与持仓同步分离；币安代币化美股永续的入场价 ≠ 美股期权视角，自动灌会把两本账混在一起。本特性只提供「底层敞口自某日起」的上下文，journal 仍是权威。

## 2. 数据流

```
fund.db binance_fills
  → 净仓走查（按 symbol+position_side 追累计净仓）
  → 当前持仓的开仓 fill_time
  → map_symbol 过滤（GOOGLUSDT→GOOGL；黑名单/非USDT 剔除）
  → snap 到最近有 RND 曲线的交易日
  → {ticker: {open_date, rnd_date, entry_price}}
  → symbol_detail API 带 binance_entry
  → 前端 detail.binance_entry
     ├─ (a) 密度图「vs 入场」叠加对照
     └─ (b) 扇形图入场竖线
```

## 3. 组件

### 3.1 后端派生 — `rnd/holdings_sync.py::entry_dates()`

- **共享净仓走查**：现有 `derive_current_holdings` 已经走查净仓判断「是否持有」。把其中的净仓累计逻辑抽成一个共享内部函数，`derive_current_holdings` 与新的 `entry_dates` 都复用，避免两份走查逻辑漂移。
- **开仓日语义**：按 `fill_time` 升序遍历某 (symbol, position_side) 的 fills，累计净仓；净仓从 0 → 非0 的那笔 fill_time 记为当前持仓开仓时刻；净仓平回 0 时清空标记。**平掉再开则取最近一次开仓**（不是最早）。
- **映射**：`map_symbol` 复用（去 USDT 后缀、黑名单剔除），结果按美股 ticker 归键。同一 ticker 多方向（LONG/SHORT）当前不会并存（对冲模式下净仓非0 的通常单向）；若并存，取净敞口方向的开仓日，另一方向忽略（本 v0 简化，记为已知限制）。
- **snap**：`rnd_date = MAX(date) FROM rnd_indicators WHERE symbol=? AND date ≤ open_date`。把日历开仓日（可能落在周末/美股假日）贴到最近一个有 RND 曲线的交易日。查不到（开仓早于该标的 RND 数据起点）→ `rnd_date = None`。
- **签名**：`entry_dates(conn, fund_db_path=FUND_DB_PATH) -> dict[str, dict]`，值为 `{"open_date": "YYYY-MM-DD", "rnd_date": "YYYY-MM-DD"|None, "entry_price": float}`。
- **降级**：`fund.db` 不存在（本机开发）→ 返回 `{}`。**只读 fund.db，绝不写**（对齐持仓同步铁律）。

### 3.2 API — `server/queries.py::symbol_detail` + `server/api.py`

- `symbol_detail(symbol)` 返回里增加 `binance_entry: {open_date, rnd_date, entry_price} | null`。
- 单标的查询时调 `entry_dates` 取该 ticker 的条目；不在持仓中 → `null`。
- 走现有 `require_auth` + `valid_symbol`，**不新增端点**。
- fund.db 读取失败/缺失 → `binance_entry: null`，不影响 symbol_detail 其余字段。

### 3.3 前端密度对照 — `web/app.js::loadCompare`

`cmpMode==="entry"` 时的对照日选取，优先级：
1. **有 journal 持仓** → 用 `myPosition.event_date`（权威，现状不变）
2. **否则有 `binance_entry.rnd_date`** → 用它，标签「币安开仓 {open_date}（虚）vs 今日（实）」
3. **都没有** → 现状退回「昨 vs 今」

密度叠加复用现成逻辑（`density?date={rnd_date}` 拉对照日曲线叠今日），零新增渲染代码。`rnd_date` 为 `null`（开仓早于数据）→ 视为无对照日，标签注「入场早于数据窗口」。

### 3.4 前端扇形图竖线 — `web/app.js::loadFan`

- 往现有 markLine 数组加一项：`{ xAxis: binance_entry.rnd_date, label: { formatter: "▲币安开仓" } }`，与 roll/gate_fail/bimodal 标记并列同一机制。
- 扇形图 `xAxis: type:"category"`：入场日 `rnd_date` 若不在当前窗口（`fanDays`，默认 120）的 dates 内 → ECharts 不渲染该 markLine（无害），旁注「入场早于窗口」。
- 扇形竖线**始终按币安开仓画**（与 journal 止损线是不同信息，可并存），不受密度对照的优先级影响。

## 4. 错误处理

| 场景 | 行为 |
|------|------|
| fund.db 缺失（本机） | `entry_dates` 返回 `{}`，`binance_entry: null`，无标记，密度退回昨 vs 今 |
| 标的未在币安持有 | 该 ticker 不在 dict，`binance_entry: null`，同上 |
| 开仓日为周末/假日 | snap 到最近 ≤ 开仓日的交易日 |
| 开仓早于该标的 RND 数据 | `rnd_date: null`；密度标「入场早于数据窗口」，扇形无竖线 |
| 开仓日在窗口外（>fanDays） | 扇形 markLine 不渲染，旁注「入场早于窗口」；密度对照仍可用（拉那天曲线） |
| journal 持仓与币安持仓并存 | 密度对照 journal 优先；扇形竖线仍按币安开仓画 |

## 5. 测试 — `tests/test_holdings_entry.py`（合成 fills，无网络）

- 单笔开仓 → `open_date` = 那笔 fill 日
- 加仓（scale-in，同向多笔）→ `open_date` = 首次 0→非0 那笔
- 平掉再开（0→非0→0→非0）→ `open_date` = **最近一次**开仓
- 已平仓（净仓回 0）→ 不在结果里
- snap：周末开仓 → `rnd_date` 贴到最近交易日（用合成 rnd_indicators 日期表）
- snap：开仓早于 RND 数据 → `rnd_date = None`
- map_symbol 过滤：黑名单（如 SPCXUSDT）不出现在结果里
- fund.db 缺失 → 返回 `{}`

## 6. 已知限制（记档，非阻塞）

- 同一 ticker 多方向并存时只取净敞口方向（v0 简化）。
- `entry_price` 是币安代币化美股永续的成交价，非美股 underlying 收盘价；仅作展示，不参与任何 RND 计算。
- 开仓日按 UTC 日历日取（fill_time 是 epoch 毫秒 UTC），与美股交易日（美东）可能差一日；snap 到 ≤ 开仓日的最近交易日已吸收此差，方向保守（不会锚到入场之后）。

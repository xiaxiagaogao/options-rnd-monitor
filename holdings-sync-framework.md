# 持仓同步 — 需求框架 v0

> 状态：**定版**（2026-07-23）。本文档是「持仓同步」专题的工作 SSOT。
> 与主 SSOT `rnd-dashboard-spec.md` 冲突时：主 spec 管管线/指标/纪律；**本专题的产品形态与调用边界以本文为准**，决议回写主 spec 前两文档并存。
> 范围：立法与边界。**不含实现排期细项。**

---

## 0. 一句话

把 **fund-dashboard**（币安 USD-M 合约的朋友基金看板）里**当前持有的标的**，每日自动映射成美股 ticker、过滤、过闸门后，**驱动 RND 仪表盘的标的池**——取代原来手动换入的动态 2 槽。

RND 只跟随**标的集合**，不搬运仓位/方向/敞口。

---

## 1. 背景与两项目关系

| 项 | 事实 |
|---|---|
| RND 仪表盘 | 美股期权 RND EOD 管线 + 可视化。生产在 VPS `<VPS_IP>`，systemd `rnd-dashboard`（uvicorn :8600），权威库 `rnd.sqlite`，cron 每日 `eod_update`。 |
| fund-dashboard | 同一 VPS 的共租 Docker 容器（:8090），Go + 纯 Go SQLite（`fund.db`，WAL）。核算几位朋友在作者币安 USD-M 合约账户里的 NAV 份额。 |
| 关键洞察 | 用户在币安交易的是**代币化美股/ETF 永续**（`AAPLUSDT`/`NVDAUSDT`/`SPYUSDT`…），底层绝大多数就是**美股**。两系统不是错配，是互补：币安不给期权/RND 视角，RND 正好补上底层美股的风险中性密度。 |
| 本专题做什么 | 在 RND 侧新增一个「持仓同步」模块，让 RND 覆盖的标的自动跟随真实币安持仓。 |
| 明确非目标 | 把仓位方向/敞口/杠杆灌进 RND 的 `trade_journal`（是另一层，本 v0 不做）。 |

---

## 2. 已决口径（速查）

1. **同步层级**：只同步**标的集合**；不碰 `trade_journal`，不搬仓位/方向/敞口。
2. **市值筛选**：RND **不做**。用户交易纪律（只做高市值）已保证输入干净，不引入市值数据源。
3. **过滤三步**：`直接映射 → 排除无美股期权 → 过 admission 闸门`。
4. **池模型**：重构为 `baseline`（对照基准，恒在）+ `holdings`（持仓驱动）。
5. **数据获取**：**只读同机 `fund.db`**，RND 自己 derive 当前净持仓；**零基金项目改动**。
6. **触发**：挂 `eod_update` 最前，每日一次。
7. **换出**：持仓平仓 → 从 `holdings` 移除、停日更；**不删已落库数据**。
8. **容量**：不设硬上限（YAGNI）；同步日志报数，撑到 ThetaData 配额再议。

---

## 3. 数据契约（fund.db，只读）

### 3.1 访问方式

| 项 | 已决 |
|---|---|
| 路径 | 可配置 `.env: FUND_DB_PATH`，VPS 默认 `<FUND_DB_PATH>`。 |
| 权限 | RND 进程（VPS 上以 root 跑 systemd）直接只读 host 路径上的 bind-mount 文件；无需进容器、无需鉴权。 |
| 打开方式 | 只读 URI `file:<path>?mode=ro`，兼容 WAL；**只 SELECT，绝不写**。 |
| 本机降级 | 本机开发无 `fund.db` → 同步模块 **no-op + 日志说明**，不报错、不影响 `eod_update` 其余步骤。 |

### 3.2 净持仓 derive（对齐基金 `positions/derive.go` 口径）

> RND 只要「当前持有哪些 symbol」，是 `derive.go` 完整生命周期算法的**简化子集**：不需要 entry/exit price、PnL、hold duration。

```text
读 binance_fills（symbol, side, position_side, qty, fill_time）
按 (symbol, position_side) 分组          # 对冲模式 LONG/SHORT 必须分开，否则抵消误判
  running = Σ signed_qty                 # BUY:+qty  SELL:-qty
  若 abs(running) > 1e-9 → 该 symbol 当前持有
输出：当前持有的 binance symbol 集合（去重）
```

- **阈值** `1e-9` 与基金 `derive.go` 归零判定一致。
- **口径限制**：基金 UI 的 OPEN 用币安实时 `positionRisk`（权威），本模块用 fills-derived open（诊断级）。差异仅在 `trades_sync` 未及时落库的窗口内；对每日 EOD 同步，最坏是新仓晚一个交易日纳入。**可接受，写入脚注。**

---

## 4. 池模型与 schema（重构：baseline + holdings）

### 4.1 角色

| 组 | 语义 | 成员 |
|---|---|---|
| `baseline` | 纯对照基准，**恒在**（即使你币安平了也保留） | `SPY`、`QQQ` |
| `holdings` | 持仓驱动，每日刷新 | 映射过滤后的当前币安持仓 |

- **`NVDA` 从原 `fixed` 移入 `holdings`**（它本就是你的持仓标的）。
- **journal pin**：任何有**未平 `trade_journal`** 的标的即使不在当前币安持仓，也**保留在池、继续日更**——保护你手动建的持仓分析（如 NVDA）不因币安平仓而断更。
- **去重**：`holdings` 映射结果里若含已在 `baseline` 的（如 `QQQUSDT`→QQQ），归 `baseline`、不重复抓。
- **废弃** `pool.py` 的 `MAX_DYNAMIC=2` 手动槽机制。

### 4.2 symbols.yaml 迁移

```yaml
# 旧
fixed:   [SPY, QQQ, NVDA]
dynamic: []                 # 手动换入，容量 2

# 新
baseline: [SPY, QQQ]        # 对照基准，恒在
holdings: []                # 持仓同步驱动，勿手动编辑（每日由同步模块重写）
pinned:   [NVDA]            # 有 open journal，强制保留（自动维护，可留手动兜底）
```

> `holdings` 由同步模块每日重写，是**派生态**而非手写态。`pinned` 由 journal 自动推导，列出便于人工核查。
>
> **有效池 = `baseline` ∪ `holdings` ∪ `pinned`（去重）。** 一个标的可同时满足多组（如 NVDA 当前既在 holdings 又在 pinned），去重后只抓一次。

---

## 5. 每日同步流程（挂 eod_update 最前）

```text
1. derive_current_holdings()            # §3.2，读 fund.db，得币安 symbol 集合
2. map + filter                         # §6：strip USDT → ticker；排除黑名单；未知靠闸门兜底
3. admission gate                       # 复用 rnd/admission.py，链质量不达标者剔除
4. diff(新 holdings 集合, 现池)
     · 新进标的  → 派 scripts/backfill.py --years 3（后台，复用现机制）
     · 消失标的  → 若无 open journal：从 holdings 移除、停日更（数据保留）
                    若有 open journal：转入 pinned、继续日更
5. 写回 symbols.yaml 的 holdings/pinned → eod_update 后续正常抓期权
```

- 幂等：重复跑同一天不产生副作用（backfill 本就幂等可续）。
- 失败隔离：同步失败**不阻断** `eod_update` 的数据刷新（与现有 push_daily 挂法一致）。
- 首日回填风暴保护：新进标的批量 backfill 是后台异步、进度可查（复用 `pool.status()` 精神）。

---

## 6. 符号映射表

### 6.1 规则

1. **默认**：`strip("USDT")` → candidate 美股 ticker。
2. **黑名单 override**（无美股期权 / 合成 / 外国股 / 商品）→ `exclude`，附原因。
3. **未知 symbol**：默认走规则 1 尝试，靠 **admission 闸门兜底**；新纳入池的标的发一条 **TG 告知**（知情、非阻塞），异常者人工补黑名单。
4. 商品→ETF 代理（`XAU`→GLD 等）：**v0 不做**（用户明确不抓商品），留接口位。

### 6.2 初始黑名单（基于已见 35 个历史标的）

| symbol | 原因 |
|---|---|
| `SAMSUNGUSDT`、`SKHYNIXUSDT` | 韩股，无美股主挂牌期权 |
| `OPENAIUSDT`、`SPCXUSDT` | 未上市（OpenAI / SpaceX 合成合约） |
| `DRAMUSDT` | 合成主题，无对应美股 |
| `XAUUSDT`、`XAGUSDT`、`CLUSDT`、`BZUSDT` | 商品（金/银/WTI/布伦特）；**`BZ` 是布伦特原油，不是美股 BZ 看准网**——映射不能纯机械 |

### 6.3 直接映射（strip 即得，能否入池由闸门定）

`AAPL AMD AMZN ARM AVGO GOOGL HOOD INTC JPM META MRVL MU NVDA TSLA TSM WMT NOK RKLB FLNC LITE SNDK SPY QQQ EWJ EWY KORU`

> `SPY`/`QQQ` 虽可 strip 映射，但归 `baseline`（§4.1 去重）；`NVDA` 归 `holdings` + `pinned`。
> 其中中小盘 / 新分拆 / 高杠杆 ETF（`RKLB` `FLNC` `SNDK` `KORU` …）预计被 admission 闸门挡下——这是**期望行为**，不是 bug。`RKLB` 是主 spec 的 FAIL 试金石。

---

## 7. 容量与保护

| 项 | 已决 |
|---|---|
| 硬上限 | **不设**（当前能抓的仅约 6 个）。 |
| 监控 | 每日同步日志报「候选 N / 映射后 M / 过闸门 K / 新增 / 换出」。 |
| 告警 | 新纳入标的 + 未知 symbol 走 TG（复用 `rnd/telegram.py`）。 |
| 触发再议 | 若某日过闸门标的数逼近 ThetaData 配额或拉长 `eod_update` 时长，再引入软上限（按名义敞口取前 N）。 |

---

## 8. 前端

| 项 | 已决 |
|---|---|
| 标签迁移 | `fixed`→`baseline`（对照基准）、`dynamic`→`holdings`（我的持仓）；契合雷达「当前标的 vs 对照基准」既有区分。 |
| 「添加标的」手动卡 | 降级为应急后门（隐藏或收进 admin），主路径改由持仓同步驱动。 |
| 同步态展示 | 来源徽标（持仓驱动 / 基准 / pinned）**v0.1 后置**，本期不做。 |

---

## 9. 代码锚点（现状 + 落点）

| 锚点 | 现状 / 落点 |
|---|---|
| `symbols.yaml` | 现 `fixed/dynamic`；改为 `baseline/holdings/pinned`（§4.2）。 |
| `server/pool.py` | 现手动 `swap_in/_write_pool/status`、`MAX_DYNAMIC=2`；重构为持仓驱动，废弃手动 2 槽（保留应急后门）。 |
| `rnd/admission.py` | 复用，作过滤步 3，无需改动。 |
| `scripts/backfill.py` | 复用，新进标的 3 年回填。 |
| `scripts/eod_update.py` | 同步模块挂其**最前**。 |
| `rnd/telegram.py` | 复用，新增/未知告警。 |
| **新增** `rnd/holdings_sync.py` | `derive_current_holdings()` + 映射黑名单 + `sync()` 编排（读 fund.db → map/filter/gate → diff → 写 yaml）。 |
| **新增** `tests/test_holdings_sync.py` | 合成 fills → derive 净持仓；映射/黑名单/未知；本机无 fund.db 降级；diff/pin/换出。 |

---

## 10. 非目标（本框架明确砍掉）

- 把仓位方向 / 敞口 / 杠杆同步进 `trade_journal`。
- RND 侧市值门槛 / 引入市值数据源。
- 商品→ETF 代理（金银油）。
- 改动 fund-dashboard 代码 / 加内部端点 / 走 JWT API。
- 实时（盘中）同步：本模块是 EOD 日频。
- 容量硬上限（除非撞配额）。

---

## 11. 细则（已定版，按倾向敲定）

| # | 决定 |
|---|---|
| H1 | **直接只读**（WAL 允许并发只读，风险低）；不做快照拷贝。 |
| H2 | `pinned` **自动为主**（open journal 推导）+ 手动可加兜底。 |
| H3 | 「添加标的」手动卡**保留为 admin 应急后门**（不删）。 |
| H4 | 前端来源徽标 **v0.1 后置**，本期不做。 |
| H5 | **实现阶段执行**：回写 `rnd-dashboard-spec.md` §1/§2 标的池语义（fixed/dynamic → baseline/holdings）。 |

---

## 12. 修订记录

| 日期 | 变更 |
|---|---|
| 2026-07-23 | v0：立法——只同步标的、读 fund.db derive、baseline+holdings 池模型、映射黑名单、EOD 挂载、journal pin、本机降级。 |
| 2026-07-23 | v1：**定版**——§11 待决项按倾向敲定（H1 直接只读 / H2 pinned 自动+手动 / H3 手动卡留 admin / H4 徽标后置 / H5 回写主 spec 留实现阶段）。 |

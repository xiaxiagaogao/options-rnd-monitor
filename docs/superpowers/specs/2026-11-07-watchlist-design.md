# 自选清单（watchlist）设计

日期：2026-11-07 · 状态：用户已确认，已实现（待部署） · 需求来源：pi-tradpod 会话转达的用户需求，关键取舍由用户在本会话确认

## 1. 背景与目标

现状：RND 池 = baseline（SPY/QQQ）+ holdings（持仓同步按 fund.db 每日增删）+ pinned（open journal）。下单后面板才会加这个标的；身份（投机/信念）另在 `holding_identity` 登记。

目标：**用户在 UI 里维护一份「自选清单」，每个标的固定一个角色；系统按清单拉期权链；持仓怎么变都不影响清单。** pi-tradpod 通过新接口 `GET /api/watchlist` 读这份清单，把其中 `active` 的标的当作固定交易名单（含角色）。

## 2. 用户决策（2026-11-07）

| # | 问题 | 决定 |
|---|---|---|
| 1 | 上线时的初始清单 | 生产当前整个池（10 只：SPY、QQQ、AMZN、GOOGL、HOOD、INTC、MU、NVDA、RKLB、SNDK），角色沿用已登记身份 |
| 2 | QQQ 角色 | 改为「指数」（身份历史追加一条「由信念改为指数」） |
| 3 | 「指数」怎么存 | `holding_identity` 加入 `index`（一次性表迁移，已有记录原样保留）；角色与身份从此是同一个东西 |

需求方已定：角色三选一 `index`（仅 SPY、QQQ）/ `conviction` / `speculative`；`index` 的失效线按信念处理（多头 Q05、空头 Q95）。

## 3. 范围外

- 持仓展示不变：标的页币安持仓卡、今日页持仓摘要、展望里的持仓、止盈止损页仍读 fund.db；只是持仓不再决定池。清单外的持仓照旧在持仓摘要里标「池外」。
- `holdings_sync` 的读取函数（`entry_dates` 等）保留；`sync()` 不再被调用（函数与测试暂留，后续清理）。
- journal 手动轨不变；它不再能「强保留」标的进池（目前无 open journal）。
- 冻结口径、闸门、准入阈值都不变。

## 4. 数据

`rnd/db.py` DDL：

```sql
-- 自选清单（watchlist spec，2026-11-07）：池的唯一来源。当前状态表，可改；改动记 watchlist_events。
CREATE TABLE IF NOT EXISTS watchlist (
  ticker     TEXT PRIMARY KEY,
  binance    TEXT NOT NULL,          -- 币安合约名，默认 ticker 去掉 "." 加 USDT
  stage      TEXT NOT NULL CHECK (stage IN ('queued','checking','rejected','backfilling','ready','error')),
  reason     TEXT,                   -- rejected / error 的原因白话
  check_json TEXT,                   -- 最近一次准入检查的指标（UI 展示）
  added_at   TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
-- 清单改动流水（只追加）：add / remove / role / binance / stage。
CREATE TABLE IF NOT EXISTS watchlist_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker   TEXT NOT NULL,
  action   TEXT NOT NULL CHECK (action IN ('add','remove','role','binance','stage')),
  detail   TEXT,                     -- JSON
  at       TEXT NOT NULL             -- ISO8601 带服务器时区偏移
);
```

- **角色不存在 watchlist 里**：当前角色 = `holding_identity` 该标的最新一行；`role_changed_at` = 那一行的 `changed_at`。避免两处存同一个事实。
- `holding_identity.identity` 的 CHECK 扩成 `('speculative','conviction','index')`。新库直接用新 DDL；已有库由迁移脚本重建表（§8）。
- 移除 = 删 watchlist 行 + 记 `remove` 事件；`rnd_*` 历史数据、身份记录都保留。

### 状态（对外 status）

| stage | status | 含义 |
|---|---|---|
| ready | `active` | 已入池、有数据，每日拉取 |
| queued / checking / backfilling / error | `pending` | 等待或正在准入检查 / 回填；error 下次自动重试，`reason` 写原因 |
| rejected | `rejected` | 准入没过，`reason` 写哪条没过；可「重新检查」或删除 |

**池 = stage 为 ready 的条目**（`server/pool.effective_symbols()` 改读 watchlist，所有下游——eod_update、overview、展望、`/api/context`——自动跟随）。回填中的标的不进池，回填完成才 active。

## 5. 准入与回填：后台 worker + ThetaData 锁

ThetaData 免费档只允许单会话，准入检查和回填都要拉链，不能和每日 eod_update 并发。

- 新增 `scripts/watchlist_worker.py`：先拿文件锁 `output/thetadata.lock`（`fcntl.flock` 排他、阻塞等待），然后循环处理 stage 为 queued / error 的条目：`checking` → `admission.check_candidate` → 不过 `rejected`（reason 写指标与门槛）/ 过 `backfilling` → 子进程跑 `backfill.py --symbols X --years 3`（可续跑）→ 有 pinned 数据则 `ready`，否则 `error`（「回填后仍无可用数据」）。处理完退出。
- 启动时把残留的 checking / backfilling（进程被杀或重启留下的）改回 queued。
- 回填期间条目被删：回填照常跑完，结束时发现行已不在就不再改状态。
- 触发：UI 加入 / 重新检查后，API 起一个 detached worker；eod_update 结束时若还有 pending 条目也起一个。多个 worker 由锁串行，后来的拿到锁后发现没活就退出。
- **eod_update 也拿同一把锁**（整个运行期间）。代价：若用户恰好在 eod 前加了标的，worker 回填（约 25–30 分钟）会让当天推送相应推迟；反过来 worker 会等 eod 跑完再开始。
- **防卡死**：eod_update 等锁最多 90 分钟，超时就不等、照常更新（撞单会话按单标的失败处理，比整天没数据好）；worker 单次回填超过 3 小时即终止并记 error。
- eod_update 不再调 `holdings_sync.sync()`、不再推「持仓同步」TG、不再派发持仓回填。

## 6. 接口

### 6.1 机器接口 `GET /api/watchlist`

与 `/api/context` 同一个 token：`Authorization: Bearer <CONTEXT_API_TOKEN>`；未配置 503，缺失/错误 401。

```json
{
  "version": "<sha256(规范化 JSON 的 entries)>",
  "updated_at": "2026-11-08T10:00:00+08:00",
  "entries": [
    {"ticker": "NVDA", "role": "conviction", "binance": "NVDAUSDT", "status": "active",
     "reason": null, "added_at": "2026-11-08T09:00:00+08:00",
     "role_changed_at": "2026-10-19T22:31:02+08:00"}
  ]
}
```

- `entries` 按 ticker 升序；`reason` 只在 pending / rejected 时非 null。
- `version` = 对 entries 做与 `/api/context` 相同的规范化 JSON（键排序、无空格）后的 sha256；内容不变则不变。
- `updated_at` = `watchlist_events` 最新一条的时间（任何增删改、状态变化都会记事件）。
- 只读，不碰 ThetaData，不读 fund.db。

### 6.2 面板接口（cookie 会话鉴权，同现有 `require_auth`）

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/watchlist/manage` | 清单 + stage、reason、准入指标、回填进度；附「持仓里有、清单里没有」的标的 |
| POST | `/api/watchlist/add` | `{ticker, role, binance?}`；校验后写 queued 并起 worker |
| POST | `/api/watchlist/update` | `{ticker, role?, binance?}`；改角色 → `set_identity` 只追加 |
| POST | `/api/watchlist/remove` | `{ticker}` |
| POST | `/api/watchlist/recheck` | `{ticker}`；rejected / error → queued 并起 worker |

校验：ticker 走 `SYMBOL_RE`；role ∈ 三选一，`index` 只许 SPY、QQQ；binance 须匹配 `^[A-Z0-9]{2,20}USDT$`；重复加入 400。

替换掉旧的「添加标的」后门：删除 `/api/pool/add`、`/api/pool/status`、`/api/admission/{symbol}`（它们在 uvicorn 里直接拉 ThetaData，绕过锁）及 `pool.swap_in`。

## 7. 前端

- 侧栏新增「自选清单」（view `watchlist`，路由 `/watchlist`）；删掉标的页侧栏里旧的「添加标的」卡片。
- 主区一张表：代码 · 角色（指数 / 信念 / 投机 分段按钮，指数只对 SPY、QQQ 可选）· 币安合约（可改）· 状态（active / 回填中 / 检查中 / 未通过 + 原因 + 重新检查）· 加入日期 · 移除。上方一行加入表单。改角色、移除先确认（改角色 = 挪失效线，会留记录）。
- 有 pending 条目时每 15 秒刷新一次。
- 标的页持仓卡的身份按钮保留（写的是同一张表）；SPY、QQQ 多一个「指数 · Q05」。
- 动手前按惯例先过 frontend-design / impeccable，做完本机截图验收（只验桌面）。

## 8. 迁移（上线一次）

`scripts/migrate_watchlist.py`，默认 dry-run，`--apply` 才写；幂等。

1. 若 `holding_identity` 的 CHECK 不含 `index`：事务内建新表、按原 event_id 拷贝全部行、删旧表、改名。
2. 若 watchlist 为空：按 `symbols.yaml` 当前有效池（baseline ∪ holdings ∪ pinned）写入，有 pinned 数据的直接 `ready`，否则 `queued`；binance 用默认名；记 `add` 事件（detail 注明 migration）。
3. 角色：SPY → `index`（原无记录）；QQQ → `index`（原信念，追加一条）；其余沿用最新身份；若有标的没有身份，dry-run 列出并中止，等人定。
4. 生产部署顺序：备份 rnd.sqlite → 停服务 → 迁移 dry-run → `--apply` → 部署代码 → 起服务。`symbols.yaml` 不覆盖（之后只剩 expiry 段在用）。

## 9. 测试

- `tests/test_watchlist.py`（临时库）：加入 / 改角色 / 改合约名 / 移除 / 重复与非法输入；`index` 只许 SPY、QQQ；status 映射；`version` 稳定（同内容同哈希，改动即变）；`effective_symbols` 只含 ready。
- worker（假准入 + 假回填）：通过 → ready；不过 → rejected 带原因；回填后无数据 → error；回填中被删不复活；残留 checking 复位。
- 迁移（临时库）：CHECK 重建保留全部行与 event_id；按 yaml 播种；SPY / QQQ → index；二次运行无变化。
- 路由：`/api/watchlist` token 鉴权（503 / 401 / 200）与契约字段；面板路由未登录 401。
- 身份：`index` 选线 Q05/Q95、文案「指数」；`identity.as_of` 迁移前 QQQ 仍回放为信念。
- 顺手修 `tests/test_assistant_context.py` 6b：读生产数据却写死「NVDA 身份未登记」，改为按是否已登记分支断言。

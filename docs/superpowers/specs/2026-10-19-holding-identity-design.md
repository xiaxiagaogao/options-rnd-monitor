# 币安持仓身份登记（投机 / 信念）设计

日期：2026-10-19 · 状态：设计已与用户确认，待实现

## 1. 背景

主 spec §6 纪律：**投机仓钉 Q25，信念仓钉 Q05**（失效线由身份决定，身份与仓位自洽）。

币安持仓由持仓同步从 fund.db 派生，没有身份字段。于是所有用到失效线的地方都"两线并挂"：
展望 / 研究助手写「身份未登记」并同时给 Q25 与 Q05（`server/assistant.py::IDENTITY_NOTE`），
标的页持仓卡同时列 Q05、Q25 两组且状态灯固定按 Q05 判，止盈止损页两条线同权重。

用户要给当前币安持仓登记身份，让这些地方只认身份对应的那一条线。

## 2. 用户决策（2026-10-19）

| # | 问题 | 决定 |
|---|---|---|
| 1 | 怎么录入 | **面板上直接选**：标的页币安持仓卡上选「投机 / 信念」 |
| 2 | 标签寿命 | **跟标的走**：身份绑定代码，平仓再开沿用，直到手动改 |
| 3 | 能否改 | **可以改，留变更记录**：每次改动记日期与前后身份，卡片与展望写明「X 日由投机改为信念」 |
| 4 | 生效范围 | **每日展望 / 研究助手、标的页持仓卡、止盈止损页**；今日页持仓摘要的灯**不改** |

存储方案选 **新建只追加的身份记录表**（相对"借用 trade_journal"——其 event_type 有 CHECK 约束、且有 journal 记录会让标的页改走 journal 卡；相对"yaml 文件"——线上状态文件易被部署覆盖，同 `symbols.yaml` 踩过的坑）。

## 3. 范围外

- 今日页持仓摘要的灯：仍按入场日 Q05（用户未选）。已知分歧：投机仓收盘在 Q05 与 Q25 之间时，标的页卡片的灯（按 Q25）亮红、今日页的灯（按 Q05）仍绿。
- 冻结口径不动：标的页持仓卡仍按入场日冻结；展望 / 止盈止损页仍按本周期冻结（`queries.cycle_freeze_date`）。
- journal 手动轨不动：journal 有开仓时仍优先，用 journal 自己的 identity。
- 不做仓位反解 / 风险预算：币安仓位已有实际数量。
- 不提供「清除回未登记」：点错直接改成另一个身份即可（改动照样留记录）。

## 4. 选线规则（唯一事实来源）

新模块 `rnd/identity.py::stop_quantile(identity, long) -> str | None`，所有消费方都调它：

| 身份 | 多头 | 空头 |
|---|---|---|
| speculative（投机） | q25 | q75 |
| conviction（信念） | q05 | q95 |
| None（未登记） | None → 两线并挂 | None → 两线并挂 |

现状里空头也套 Q25/Q05、只把比较方向反过来，这是错的（当前 13 个持仓全为多头，无实际影响），借此一并改为镜像分位。未登记时「两条线」对空头即 q75 + q95。

## 5. 数据

`rnd/db.py` 的 DDL 追加：

```sql
-- 币安持仓身份登记（2026-10-19）：跟标的走、只追加。当前身份 = 该标的 event_id 最大的一行。
CREATE TABLE IF NOT EXISTS holding_identity (
  event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol        TEXT NOT NULL,
  identity      TEXT NOT NULL CHECK (identity IN ('speculative','conviction')),
  prev_identity TEXT,            -- 改动前身份；首次登记为 NULL
  changed_at    TEXT NOT NULL    -- 改动时刻，ISO8601 带服务器时区偏移（VPS +08:00）
);
```

- 只 INSERT，不 UPDATE / DELETE。与当前身份相同的设置是 no-op，不写行。
- 与 trade_journal 一样是用户录入、不可派生的数据，随 `rnd.sqlite` 一起备份。
- 表在 `db.get_conn()` 建库时自动创建，无数据迁移。

## 6. 模块与接口

### 6.1 `rnd/identity.py`（新）

- `stop_quantile(identity, long)`：见 §4。
- `current(conn) -> dict[str, dict]`：`{symbol: {identity, prev_identity, changed_at}}`，每个标的最新一行。
- `history(conn, symbol) -> list[dict]`：该标的全部记录，新的在前。
- `set_identity(conn, symbol, identity) -> dict`：校验 identity ∈ {speculative, conviction}（否则 `ValueError`）；与当前相同则不写；否则插入一行（prev_identity = 原身份）。返回 `{symbol, identity, prev_identity, changed_at, changed: bool}`。
- `change_note(rec) -> str`：白话，如「10-19 登记为信念」「10-25 由投机改为信念」，供卡片与展望共用。

### 6.2 `server/api.py`

`POST /api/holdings/identity`，`dependencies=[Depends(require_auth)]`（同 journal 写接口）。
请求体 `{symbol, identity}`；symbol 须是当前币安持仓（`holdings_sync.entry_dates` 里有），否则 400「当前无币安持仓」；identity 非法 400。
返回 `set_identity` 结果 + `history`。

### 6.3 `server/queries.py`

- `symbol_detail`：`binance_entry` 增加 `identity`（当前身份或 None）、`stop_q`（`stop_quantile` 结果）、`identity_history`（新的在前）。
- `exit_curves`：返回体增加 `identity` 与 `stop_q`（无持仓则均为 None），供前端高亮。

### 6.4 `server/assistant.py::_binance_position`

- 读 `identity.current`，`stop_q = stop_quantile(identity, long)`。
- **已登记**：`identity` / `stop_q` 填实；`identity_note` = 「身份：信念（钉 Q05）；<change_note>」；`stop_lines` **只含身份那一条**（键为身份名）。
- **未登记**：保持现状（identity None、`IDENTITY_NOTE`、两条线），只是空头改用镜像分位。
- 系统提示词（「有持仓的标的」一节）补一条：身份已登记的持仓只按 `stop_lines` 那一条判失效与顺逆风，写明身份；`identity_note` 含改动记录时照写。

### 6.5 前端（`web/app.js` + `web/index.html`）

- **标的页币安持仓卡**：
  - 身份控件：两档切换「投机 · Q25 / 信念 · Q05」（空头显示 Q75 / Q95）；未登记时两档都不选中并标「身份未登记」。
  - 首次登记直接保存；**改动**（已有身份 → 另一个）先弹确认：「把 X 从投机改为信念？失效线将由 Q25 改为 Q05，改动会留记录。」
  - 控件旁显示最近一条 `change_note`。
  - 身份对应那组（冻结 / 实时 / 偏移）正常显示，另一组变淡；未登记两组同权重（同现状）。
  - 状态灯按身份那条线判（`beBreached` 改用 `stop_q`），文案如「已跌破入场日 Q25（投机线）」；未登记仍按 Q05（同现状）。
- **止盈止损页**：`exitMarks` 的 cost 曲线上，身份那条线加深并标「失效线」，另一条变淡；未登记同现状。
- **今日页**：不改。
- 实现前按全局约定先过 UI skill（frontend-design → impeccable），沿用页面现有配色与控件风格。

## 7. 错误处理

- POST 非法身份 / 非当前持仓 → 400，前端提示错误、保持原状态不变。
- 写库失败 → 500，前端同上。
- fund.db 读不到 → `binance_entry` 为 None，卡片不出现，无从登记；展望仍走 `open=None` 分支（不变）。
- 已不再持有的标的，其身份记录保留，重新开仓时自动沿用（跟标的走）。

## 8. 测试与验收

- `tests/test_holding_identity.py`（新，临时库）：首次登记、相同值 no-op、改动记 prev、history 顺序、非法身份报错、`stop_quantile` 全矩阵（含空头）、`change_note` 文案。
- 展望上下文（扩 `tests/test_assistant_holdings.py`）：已登记信念 → `stop_lines` 只有 q05 一条、note 含登记日；改过身份 → note 含「由投机改为信念」；未登记 → 与现状一致；空头镜像。
- 接口：`POST /api/holdings/identity` 的 400 分支与成功路径；`symbol_detail` / `exit_curves` 返回新字段。
- 前端：本机免鉴权 dev server + headless 截图（只验桌面）：登记、改动确认、淡化与灯文案、止盈止损页高亮；控制台零错误。
- 部署：scp 改动文件 → 重启 → 三方校验；首登记由用户在面板上完成。

## 9. spec 回写

主 spec §6「仓位反解」条后补一句：币安持仓的身份在标的页登记（`holding_identity` 表，跟标的走、改动留痕），未登记时两线并挂。

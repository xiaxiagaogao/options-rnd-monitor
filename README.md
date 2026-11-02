# options-rnd-monitor

美股期权 **RND（风险中性密度）** EOD 管线 + 可视化仪表盘 + 研究助手。

从期权链反推市场对标的未来价格的风险中性概率分布，落库、算指标（分位/偏度/尾部/状态分位）、可视化；可选地推送 Telegram 异动/市场展望，并跟随币安合约基金的当前持仓自动纳管标的池。

> **这是一个运行框架（“壳”）。** 不含任何行情/持仓数据与密钥——clone 后需自己配置数据源与密钥、自己拉取历史数据，才能运行。

## 依赖

- Python 3.12+，`pip install -r requirements.txt`
- **ThetaData** 订阅（期权 EOD 数据源，核心必需）
- 可选：Polygon（辅源）、Telegram Bot（推送）、OpenAI 兼容 LLM 中转（研究助手）、币安基金看板 `fund.db`（持仓同步）

## 配置

复制 `.env.example` → `.env`，按注释填入你自己的密钥/配置：

```bash
cp .env.example .env
# 然后编辑 .env
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 回填历史（首次，按标的拉数年 EOD）
.venv/bin/python scripts/backfill.py --symbols SPY,QQQ,NVDA --years 3

# 每日增量更新（美股收盘后；含研究助手推送、持仓同步）
.venv/bin/python scripts/eod_update.py

# 仪表盘
.venv/bin/uvicorn server.api:app --port 8600
```

## 时间点上下文 API（O1）

供外部交易/回放程序按日期读取行情侧 RND 上下文。**只读、不调 LLM、不含现货持仓信息**（数量、均价、journal 未平仓、币安持仓卡都不返回）。

- 请求：`GET /api/context/{symbol}?asof=YYYY-MM-DD`；`asof` 可省略（= 最新 RND 日）。
- 鉴权：请求头 `Authorization: Bearer <CONTEXT_API_TOKEN>`。环境变量未配置 → 一律 `503`；token 缺失/错误 → `401`。该 token 只作用于本路由，面板的 cookie 会话不受影响。
- 日期语义：`asof` 是请求的日期；`date` 是实际使用的 RND 日（`≤ asof` 的最近 pinned 交易日，没有则 `null`）。`asof` 早于该标的第一条 RND → `200`，`date` / `distribution` 为 `null`。
- `distribution` 用的是与 Q05/Q25/Q75/Q95 失效线**同一条曲线**：`rnd_indicators` 的 pinned 行（最接近 30 DTE 的到期）对应的 `rnd_curve` 网格；`strikes` 升序，`cdf` 等长。
- `identity` 取 `holding_identity` 中 `changed_at` 不晚于 `asof` 当天结束的最新一行。
- `context_hash`：响应里除 `context_hash` 和 `generated_at` 之外的全部字段做规范化 JSON（`sort_keys`、无空格、浮点按 Python `repr`）后的 sha256；同样的内容任何时候请求都得到同一个哈希，调用方用它判断内容是否变了。
- `identity` 按美东日期判定：`changed_at` 换算到 America/New_York 后的日期 ≤ `asof` 的最新一行。
- 错误：非法 symbol / 不在池中 → `404`；`asof` 格式错 → `400`。

示例（数字全是合成值）：

```bash
curl -H "Authorization: Bearer $CONTEXT_API_TOKEN" \
  "http://127.0.0.1:8600/api/context/TST?asof=2026-08-05"
```

```json
{
  "symbol": "TST",
  "asof": "2026-08-05",
  "date": "2026-08-05",
  "gate_pass": true,
  "distribution": {
    "asof": "2026-08-05",
    "expiry": "2026-08-05",
    "forward": 100.0,
    "strikes": [80.0, 90.0, 100.0, 110.0, 120.0],
    "cdf": [0.02, 0.16, 0.5, 0.84, 0.98]
  },
  "identity": {"identity": "speculative", "changed_at": "2026-08-04T09:30:00+08:00"},
  "context": {"meta": {"symbol": "TST", "asof": "2026-08-05"}, "...": "同 build_context，但无 position 键"},
  "context_hash": "<sha256>",
  "generated_at": "2026-08-05T23:59:59+00:00"
}
```

## 结构

| 目录 | 内容 |
|---|---|
| `rnd/` | 管线核心：拉取 / 清洗 / 拟合密度 / 指标 / 状态层 / 准入 / 持仓同步 |
| `server/` | FastAPI + 查询层 + 研究助手 |
| `web/` | 无构建 Vue3 仪表盘（vendor 本地化） |
| `scripts/` | 回填 / 每日更新 / 推送 |
| `tests/` | 脚本风格测试（`.venv/bin/python tests/test_x.py`） |
| `*-framework.md` / `docs/` | 设计文档 |

## 纪律

RND 是**数据描述，不构成投资建议**；风险中性概率 **≠** 真实概率。研究助手只读、不下单、不表方向。

## 许可

[MIT](LICENSE)

---

🤖 与 [Claude Code](https://claude.com/claude-code) 结对开发。

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

---

🤖 与 [Claude Code](https://claude.com/claude-code) 结对开发。

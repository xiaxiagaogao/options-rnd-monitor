"""研究助手：结构化上下文装配器（framework §3）。

铁律（framework §3.1）：LLM 只消费本层装配好的结构化数据，不另算。
本模块只读五表、复用 queries 的确定性读数，产出「上下文包」；不调用任何模型。
"有则填、无则显式 null + 原因"（framework §3.3）。
"""
import json
import os

from rnd.state import STATE_INDICATORS

from . import queries as q

_QUANTS = ("q05", "q25", "q50", "q75", "q95")

# LLM 接缝配置（framework §4 / T1）。用户有 Anthropic/OpenAI 兼容中转，接入=填这三行 .env。
ASSISTANT_MODEL = os.getenv("ASSISTANT_MODEL", "claude-opus-4-8")
# 质量优先：默认开满 reasoning，不计预算（用户定调）。effort 可用 .env 调到 xhigh/max。
ASSISTANT_EFFORT = os.getenv("ASSISTANT_EFFORT", "high")


class AssistantNotConfigured(RuntimeError):
    """未选定模型 / 未配密钥。端点据此返回 503，而非静默或伪造答案。"""


class AssistantError(RuntimeError):
    """LLM 调用成功但产出不可用（如思考耗尽预算、正文为空）。端点返回 502。"""

# 系统提示：模型无关的纪律与输出模板（framework §1.1 九纪律 / §3.1 数据铁律 /
# §3.4 输出模板 / §5 拒答）。措辞可调，但纪律不得删——测试锚住关键词。
SYSTEM_PROMPT = """\
你是「研究助手」，架在美股期权 EOD 风险中性密度（RND）世界地图之上。
你的作答是**数据描述，不构成投资建议**；你不是持牌顾问，不替用户决策。
**风险中性（RN）≠ 真实概率**：RN 含风险溢价、尾部偏肥；一切概率读数都以此为限，
中心钉远期（forward），不表方向涨跌观点。

## 数据铁律（唯一数值来源）
- 你只能引用「上下文包」里已给的字段、单位、已算分位与 Δ。**数值只能取自包内，不得自算。**
- 禁止心算或重算 IV、密度、分位、σ、PIT；禁止改写数值或凭空 invent 未提供的字段。
- 包里没有的（如 OI、GEX、VRP、财报日历），直说「本系统无此数据」，不推测、不脑补。

## 九条纪律
1. 冻结线写入后不可变；偏移量 (现Q−冻结Q)/σ1 是加减仓信息，不是重画失效线的借口。
2. 分位线只做**收盘确认**的论点失效线；禁止当作盘中硬止损。
3. 仓位身份：投机→Q25 止损，信念→Q05 止损，不混用。
4. roll 为预定义重钉；不得用 roll 挪动失效线。
5. 目标价高于冻结 Q95 须有书面理由，否则不予背书（那是在下市场赔率<5% 的注）。
6. RN≠真实；不表方向观点；个股异常须配 252 日分位 + 指数并排一同陈述。
7. **闸门是质量验收、不是交易信号**；gate FAIL 时说明静默/置灰原因，绝不据此编造信号。
8. 样本 sample_n<60 或 pct 为 null → 不得陈述假精确的分位排名。
9. 可信度排序：Q25–Q75 > Q05/Q95 > 偏度 > 峰度。逐分位 in_range=False 的分位落在
   **外推区**，须显式降级披露（如「Q25/Q75 报价区内可信，Q05/Q95 在外推区、按兜底口径」）。

## 拒答与降级
- 要求预测涨跌方向 → 拒绝方向观点；可改为描述 RN 分布形态与分位状态。
- 要求盘中硬止损 / 下单 / 改冻结线 → 引用纪律拒答。
- 闸门 FAIL → 只解释质量问题与 diagnostics 摘要，状态类分位按静默处理。
- 上下文缺字段 → 显式说缺，不编造。

## 输出模板
- 标题带**数据口径日期**（asof）。
- 主体用「维度 | 读数 | 白话」结构；每个读数必须逐一对应上下文包里的字段。
- 需要公式时写「公式 + 翻译：」，公式只解释已给的数、不引入新计算。
- 末尾附纪律引用块（历史统计·非预测·RN≠真实）与脚注（样本量 / 闸门状态 / 外推区落区分位）。
- 可给「还可以看」的追问建议，但不自动发起。
"""


def build_messages(ctx: dict, question: str) -> dict:
    """把上下文包 + 用户问题组装成模型无关的单次请求报文（framework §4）。

    返回 {"system", "user"}；多轮 = 多次单次，每轮由服务端用新 ctx 重新组装（本函数纯函数）。
    """
    packed = json.dumps(ctx, ensure_ascii=False, indent=2, default=str)
    meta = ctx.get("meta", {})
    user = (
        f"以下是 {meta.get('symbol')} 在数据日 {meta.get('asof')} 的结构化上下文包"
        f"（唯一数值来源，禁止另算）：\n\n```json\n{packed}\n```\n\n"
        f"用户问题：{question}\n\n"
        f"请依系统提示的纪律与输出模板作答。"
    )
    return {"system": SYSTEM_PROMPT, "user": user}


def generate(system: str, user: str, *, max_tokens: int = 16000) -> str:
    """单次请求（framework §4）：装配好的提示 → LLM → 文本。这是唯一的模型接缝。

    走官方 anthropic SDK，base_url 指向用户的兼容中转（.env: ASSISTANT_BASE_URL /
    ASSISTANT_API_KEY / ASSISTANT_MODEL）。未装 SDK 或未配密钥 → AssistantNotConfigured。
    若中转是 OpenAI 报文形，改这一个函数即可，其余各层不动。
    """
    api_key = os.getenv("ASSISTANT_API_KEY")
    if not api_key:
        raise AssistantNotConfigured(
            "研究助手未配置：请在 .env 设 ASSISTANT_API_KEY（及可选 ASSISTANT_BASE_URL / "
            "ASSISTANT_MODEL）指向你的中转接口")
    try:
        import anthropic
    except ImportError as e:
        raise AssistantNotConfigured(
            "未安装 anthropic SDK：请 `.venv/bin/pip install anthropic`，"
            "或改写 generate() 走你的 OpenAI 兼容客户端") from e

    client = anthropic.Anthropic(api_key=api_key, base_url=os.getenv("ASSISTANT_BASE_URL") or None)
    kwargs = dict(model=ASSISTANT_MODEL, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}])
    try:
        try:
            # 开满深度思考（Opus 4.8 需显式 adaptive 才开；Fable 5 always-on）。
            resp = client.messages.create(
                **kwargs, thinking={"type": "adaptive"}, output_config={"effort": ASSISTANT_EFFORT})
        except anthropic.BadRequestError:
            # 中转/模型不认 thinking·effort → 降级为普通调用。
            resp = client.messages.create(**kwargs)
    except anthropic.APIError as e:
        # 模型 id 错、鉴权失败、中转故障等统一转 AssistantError（端点 → 502），不漏成 500。
        raise AssistantError(f"模型调用失败：{getattr(e, 'message', str(e))}") from e
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    if not text.strip():
        # 思考型模型把预算耗在 thinking 上、text 为空时不静默返回空串。
        raise AssistantError(
            f"模型 {ASSISTANT_MODEL} 未产出正文（stop_reason={resp.stop_reason}）"
            "——多为思考耗尽 max_tokens；请调大 max_tokens 或换非思考型模型")
    return text


def build_context(symbol: str, asof: str | None = None) -> dict:
    symbol = symbol.upper()
    c = q.conn()
    asof = asof or q.latest_date(c, symbol)
    ind = q._row(
        c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
        (symbol, asof))
    states = q.state_at(c, symbol, asof) if asof else []
    bench = q.benchmark_of(symbol)
    bench_states = q.state_at(c, bench, asof) if asof else []
    positions = q.open_positions(c, symbol)
    close_row = q._row(
        c, "SELECT underlying_close FROM raw_chain WHERE symbol=? AND date=? LIMIT 1",
        (symbol, asof)) if asof else None
    current_close = close_row["underlying_close"] if close_row else None
    c.close()

    meta = {
        "symbol": symbol,
        "asof": asof,
        "pinned_expiry": ind["expiry"] if ind else None,
        "dte": ind["dte"] if ind else None,
    }
    return {
        "meta": meta,
        "location": _location(ind),
        "state": _state(states),
        "quality": _quality(ind),
        "benchmark": _benchmark(bench, bench_states),
        "position": _position(ind, positions, current_close),
        "events": _events(symbol, asof),
        "summary": _summary(symbol, asof, ind),
    }


def _location(ind: dict | None) -> dict:
    if ind is None:
        return {"available": False, "reason": "该 asof 无 pinned 指标行"}
    return {
        "available": True,
        "forward": ind["forward"],
        "sigma1_abs": ind["sigma1_abs"],
        "sigma1_pct": ind["sigma1_pct"],
        "quantiles": {k: ind[k] for k in _QUANTS},
        "mode": ind["mode"],
        # 逐分位报价区标志（framework §3.3 口径校正）：非单一布尔。
        "in_range": {k: bool(ind[f"{k}_in_range"]) for k in _QUANTS},
    }


def _state(states: list[dict]) -> list[dict]:
    """10 个状态指标 × value/pct/dpct/sample_n + saying/label，按 STATE_INDICATORS 排序。"""
    by_ind = {s["indicator"]: s for s in states}
    out = []
    for ind_name in STATE_INDICATORS:
        s = by_ind.get(ind_name, {"indicator": ind_name, "value": None,
                                   "pct": None, "dpct": None, "sample_n": None})
        out.append({
            "indicator": ind_name,
            "value": s.get("value"),
            "pct": s.get("pct"),
            "dpct": s.get("dpct"),
            "sample_n": s.get("sample_n"),
            "label": q.LABELS.get(ind_name, ind_name),
            "saying": q.SAYINGS.get(ind_name, ""),
        })
    return out


def _quality(ind: dict | None) -> dict:
    """闸门是质量验收、非交易信号（framework §1.1-7）；FAIL 时状态类按静默规则处理。"""
    if ind is None:
        return {"available": False, "reason": "该 asof 无 pinned 指标行"}
    return {
        "available": True,
        "gate_pass": bool(ind["gate_pass"]),
        "gate_detail": json.loads(ind["gate_detail"]) if ind.get("gate_detail") else {},
        "n_modes": ind["n_modes"],
        "modes": json.loads(ind["modes_json"]) if ind.get("modes_json") else [],
    }


def _benchmark(bench: str, bench_states: list[dict]) -> dict:
    """指数基准并排状态（framework §3.3；异动须与指数并排 §1.1-6）。"""
    pct = {s["indicator"]: s["pct"] for s in bench_states}
    return {
        "symbol": bench,
        "state_pct": {ind_name: pct.get(ind_name) for ind_name in STATE_INDICATORS},
    }


def _position(ind: dict | None, positions: list[dict], current_close) -> dict:
    """开仓监控（framework §1.1-1..5 铁律；§2.1 罗盘读数）。

    - 冻结线不可变；偏移量 = (现 Q − 冻结 Q)/冻结 σ1，是加减仓信息、不重画失效线。
    - 失效判定收盘确认制：多头 close < 冻结止损分位 → invalidated（空头反向）。
    """
    if not positions:
        return {"open": False}
    p = positions[0]  # 单用户单标的按最新开仓事件
    sig = p.get("frozen_sigma1")
    offset = None
    if ind is not None and sig:
        offset = {k: (ind[k] - p[f"frozen_{k}"]) / sig for k in _QUANTS}
    stop_q = p.get("stop_q")
    stop_level = p.get(f"frozen_{stop_q}") if stop_q else None
    invalidated = None
    if stop_level is not None and current_close is not None:
        long = (p.get("direction", "long") == "long")
        invalidated = (current_close < stop_level) if long else (current_close > stop_level)
    return {
        "open": True,
        "position_id": p.get("position_id"),
        "identity": p.get("identity"),
        "direction": p.get("direction"),
        "stop_q": stop_q,
        "entry_price": p.get("entry_price"),
        "size": p.get("size"),
        "target_price": p.get("target_price"),
        "target_rationale": p.get("target_rationale"),
        "frozen": {k: p.get(k) for k in (
            "frozen_expiry", "frozen_forward", "frozen_sigma1",
            "frozen_q05", "frozen_q25", "frozen_q50", "frozen_q75", "frozen_q95")},
        "offset": offset,
        "stop_level": stop_level,
        "current_close": current_close,
        "invalidated": invalidated,
    }


def _events(symbol: str, asof: str | None) -> list[dict]:
    """近窗事件，与仪表盘 events 同源口径（framework §3.3）。"""
    if not asof:
        return []
    return q.events(symbol, days=90).get("events", [])


def _summary(symbol: str, asof: str | None, ind: dict | None) -> dict:
    """可选标量摘要：PIT 标量 + 闸门 FAIL 时的 diagnostics 摘要。

    全网格曲线、原始链默认不进包（framework §3.3）——此处只取标量，剔除 points/curve。
    """
    pit = q.pit(symbol) if asof else {"ok": False, "reason": "无数据日"}
    gate_diagnostics = None
    if ind is not None and not bool(ind["gate_pass"]):
        d = q.diagnostics(symbol, asof, ind["expiry"])
        if d.get("ok"):
            gate_diagnostics = {"meta": d["meta"], "gate_detail": d["gate_detail"]}
        else:
            gate_diagnostics = {"ok": False, "error": d.get("error")}
    return {"pit": pit, "gate_diagnostics": gate_diagnostics}

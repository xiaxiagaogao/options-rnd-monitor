"""研究助手：结构化上下文装配器（framework §3）。

铁律（framework §3.1）：LLM 只消费本层装配好的结构化数据，不另算。
本模块只读五表、复用 queries 的确定性读数，产出「上下文包」；不调用任何模型。
"有则填、无则显式 null + 原因"（framework §3.3）。
"""
import json
import os
import re
from pathlib import Path

from rnd.state import STATE_INDICATORS

from . import queries as q

_QUANTS = ("q05", "q25", "q50", "q75", "q95")

# 币安仓位没有投机/信念登记：纪律 3 两条线都给，不替用户选（2026-10 用户定）。
IDENTITY_NOTE = ("身份未登记：币安仓位没有投机/信念登记。纪律 3 的两条线（投机→Q25、信念→Q05）"
                 "都列出并各自标注，不替用户假设身份。")

# LLM 接缝配置（framework §4 / T1）。用户有 Anthropic/OpenAI 兼容中转，接入=填这三行 .env。
ASSISTANT_MODEL = os.getenv("ASSISTANT_MODEL", "claude-opus-5-5")
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
- **方向以白话为准**：state[].reading、benchmark.state_reading、events[].reading 是代码按指标定义
  （definition）预写的方向描述。贵/便宜、左偏轻/重、倒挂与否一律以它为准，**不得自行从分位数推断方向**
  ——P 高只表示原始值大，不等于「X 贵」。例：rr25 = IV(25Δ 看涨) − IV(25Δ 看跌)，
  指数 rr25 分位高 = 看跌保护相对看涨便宜，不是 put 贵。

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

## 输出（先判断、再展开；综合不复述面板）
- **第一句就是答案/净判断**：直接回答问的那件事，或给这个标的一句有态度的净读，别开场白铺垫。
- **综合，不复述**：用户在面板已能看到分位数字；你的价值是把跨指标 / 跨历史 / 跨指数的信号连成判断——点名不对称、给「若…则…」情景。只在能佐证判断时引数，别为填表而罗列。
- 结构随问题走：**问得窄就白话简答，问得宽才展开**；需要时才用「维度 | 读数 | 白话」表，别硬套模板。
- 持仓类问题：给对论点的**顺逆风判定**（顺风 / 逆风 / 中性 + 为什么），挂失效线与偏移。
- 公式只解释已给的数（「公式 + 翻译：」）。
- 纪律**按需引用**（与本次读数相关才提），不逐条堆砌；末尾脚注按需披露样本量 / 闸门状态 / 外推区落区分位。
- **红线**：可判定定价环境偏向 / 顺逆风，**禁止**买 / 卖 / 加 / 减 / 对冲 / 仓位动作与涨跌预测——那个键用户自己按。
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
        "直接回答这个问题：先给净判断/结论，再用最相关的读数佐证（综合、点名不对称、"
        "必要时给「若…则…」），别复述面板、别硬套整张表。深度随问题走。守全部纪律与红线"
        "（不下单、不表方向、RN≠真实）。"
    )
    return {"system": SYSTEM_PROMPT, "user": user}


def build_packs(symbols: list[str]) -> dict:
    """多标的上下文包 {symbol: ctx}；币安持仓只读一次共用。"""
    holdings = load_holdings()
    return {s: build_context(s, holdings=holdings) for s in symbols}


def build_digest_messages(symbols: list[str], packs: dict | None = None) -> tuple[dict, str | None]:
    """多标的 → 一条 TG 纯文本综合收盘摘要（framework §2.3；用户选"一条综合摘要"）。

    复用九纪律 SYSTEM_PROMPT，只把 ask 换成"多标的短摘要、纯文本无表格"。返回 (messages, asof)。
    """
    packs = packs if packs is not None else build_packs(symbols)
    asof = next((p["meta"]["asof"] for p in packs.values() if p["meta"].get("asof")), None)
    body = json.dumps(packs, ensure_ascii=False, indent=2, default=str)
    user = (
        f"以下是 {', '.join(symbols)} 在数据日 {asof} 的结构化上下文包"
        f"（唯一数值来源，禁止另算）：\n\n```json\n{body}\n```\n\n"
        "任务：出一条 **Telegram 纯文本** 的收盘综合摘要（不是单标的长报告）。要求：\n"
        "- 每标的 2–4 行：钉的到期/DTE、F 与 ±1σ%、最值得看的 1–2 个状态分位"
        "（注明 P 分位与日变 Δ）、闸门状态；有持仓的标的补一行冻结失效线与偏移；"
        "今日有异动（分位穿越 P90/P10、双峰、持仓偏移>1σ）的点出来。\n"
        "- **纯文本，不要 markdown 表格 / # / **加粗**（TG 不渲染）**，用 ·、—、缩进和换行组织，可用少量 emoji。\n"
        "- 守全部纪律：RN≠真实、不表方向、样本<60 不报分位、闸门 FAIL 静默、逐分位 in_range=False 降级披露。\n"
        "- 末尾一行免责（数据描述非建议）。整体简洁，手机一两屏读完。"
    )
    return {"system": SYSTEM_PROMPT, "user": user}, asof


def build_outlook_messages(symbols: list[str], packs: dict | None = None) -> tuple[dict, str | None]:
    """全量市场展望（C++ 决断度；2026-07-22 两版原型人工锁定）。

    比 digest 更进一步：板块基调总纲 + 每标的精悍净判断 + 对已有持仓论点的顺逆风判定
    + 情景触发。红线焊死——描述定价环境偏向可以，给买卖/加减/对冲/仓位动作不行、不预测涨跌。
    复用九纪律 SYSTEM_PROMPT。packs 由调用方传入时可复用于 generate_checked 的方向自检。
    返回 (messages, asof)。
    """
    packs = packs if packs is not None else build_packs(symbols)
    asof = next((p["meta"]["asof"] for p in packs.values() if p["meta"].get("asof")), None)
    body = json.dumps(packs, ensure_ascii=False, indent=2, default=str)
    user = (
        f"以下是 {', '.join(symbols)} 在数据日 {asof} 的结构化上下文包"
        f"（唯一数值来源，禁止另算）：\n\n```json\n{body}\n```\n\n"
        "任务：出一版**市场展望（market outlook）**，C++ 决断度——够狠、有净判断，但不下单。"
        "别复述面板数字，你的价值是综合与解读。\n\n"
        "【开篇总纲（1-2 句）】\n"
        "先给一句**板块基调**：把标的按板块归拢（宽基 / 科技·纳指 / 半导体 等），"
        "一句点出今天市场是什么定价基调、担忧集中在哪个层级。像一句能转发的头条，有态度。\n\n"
        "【每标的：3-4 行，精悍别啰嗦】\n"
        "- **净判断（狠、决断）**：定价环境是什么性质，且在往哪走（升级/恶化/缓和/转向/维持）"
        "——不要「偏X」的温吞，要「X，且在Y」。\n"
        "- 支撑：只挑 1-2 个最有信息量的读数论证（点名不对称、跨指数对照、日环比），别铺全指标。"
        "方向（贵/便宜、左偏轻/重）照 reading 写，不从 P 分位自己推。\n"
        "- **情景触发**：一条「若…则…」。\n"
        "- 有持仓的标的：这套定价对你的论点是**逆风 / 顺风 / 中性**（挂失效线、偏移）"
        "——判定顺逆风可以，给动作不行。\n"
        "  · 身份未登记（identity 为 null）的持仓：投机线 Q25 与信念线 Q05 两条都挂、"
        "注明「身份未登记」，不替用户选。\n"
        "  · 失效线按包内 frozen_basis 写明口径（本周期冻结 + 冻结日）。\n"
        "  · position.open 为 null = 持仓状态未知：照实说，不写成无持仓。\n\n"
        "【红线——展望不是指令】\n"
        "- 允许：决断的环境净判断、点名不对称、情景触发、**对已有论点/持仓的顺逆风判定**"
        "（如「这对多头是逆风」）。\n"
        "- 禁止：买/卖/加/减/对冲/开平仓/仓位大小 的动作；预测涨跌点位或概率。"
        "「环境对你论点是逆风」能说，「所以你该减仓」不能说——那个键用户自己按。**不下单。**\n"
        "- 守纪律：RN≠真实（偏向是「市场在这么定价」，非「真会这样」）；闸门 FAIL 静默；"
        "样本<60 不报分位；逐分位 in_range=False 降级披露。\n\n"
        "【形式】纯文本、精悍有节奏；开篇总纲 + 每标的 3-4 行；末尾一行免责。整体短而狠。"
    )
    return {"system": SYSTEM_PROMPT, "user": user}, asof


def generate(system: str, user: str, *, max_tokens: int = 32000) -> str:
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

    # 单次生成就要几分钟：超时 / 5xx 只重试 1 次（SDK 默认 2 次，最坏要等 3×900s）。
    client = anthropic.Anthropic(api_key=api_key, base_url=os.getenv("ASSISTANT_BASE_URL") or None,
                                 max_retries=1)
    # max_tokens 由 thinking + 正文共用：2026-10 实测 Opus 全池展望 16000 吃满（15999/16000），
    # 故放到 32000。超过 ~21k 时 SDK 会拒绝默认超时的非流式请求，显式给 timeout 即放行；
    # 读超时放宽到 900s，连接超时保持 SDK 原来的 5s（中转不通时快速失败）。
    kwargs = dict(model=ASSISTANT_MODEL, max_tokens=max_tokens, system=system,
                  messages=[{"role": "user", "content": user}],
                  timeout=anthropic.Timeout(900.0, connect=5.0))
    try:
        try:
            # 深度思考：adaptive + effort（.env ASSISTANT_EFFORT）。
            resp = client.messages.create(
                **kwargs, thinking={"type": "adaptive"}, output_config={"effort": ASSISTANT_EFFORT})
        except anthropic.BadRequestError:
            # 中转/模型不认 thinking·effort → 降级为普通调用。
            resp = client.messages.create(**kwargs)
    except anthropic.APIError as e:
        # 模型 id 错、鉴权失败、中转余额不足（429）等统一转 AssistantError（端点 → 502），不漏成 500。
        # 不换模型重试（用户定）：失败就失败，推送跳过展望。
        raise AssistantError(f"模型调用失败：{getattr(e, 'message', str(e))}") from e
    u = getattr(resp, "usage", None)
    print(f"[assistant] {ASSISTANT_MODEL} stop={resp.stop_reason} "
          f"in={getattr(u, 'input_tokens', '?')} out={getattr(u, 'output_tokens', '?')}/{max_tokens}",
          flush=True)
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    if not text.strip():
        # 思考型模型把预算耗在 thinking 上、text 为空时不静默返回空串。
        raise AssistantError(
            f"模型 {ASSISTANT_MODEL} 未产出正文（stop_reason={resp.stop_reason}）"
            "——多为思考耗尽 max_tokens；请调大 max_tokens 或调低 ASSISTANT_EFFORT")
    if resp.stop_reason == "max_tokens":
        # 正文写到一半被截断：照发但明示，不当成完整稿。
        text += "\n\n⚠ 输出触顶 max_tokens 被截断，以上内容不完整。"
    return text


# ---------- 方向反例自检（2026-10：展望连续一周把指数 rr25 P99 写成「put 翼极贵」）----------

_INDEX_SYMBOLS = ("SPY", "QQQ")
_INDEX_WORDS = re.compile(r"宽基|指数|大盘")
_STOCK_WORDS = re.compile(r"个股|成分")
_CLAUSE = re.compile(r"[，。；！？,;!?\n]")
_NC = r"[^，。；！？,;!?\n]"   # 子句内任意字符
# (反例说法, 条件)：归属标的的条件全部成立时，该说法与 reading 相反。只抓「极贵 / 到顶 / 全押」
# 这类水平断言，不抓「在变贵」这类日变说法——分位从 P99 掉到 P80 时「左偏在加深」是对的；
# 也不抓光秃秃的「put 翼贵」——rr25<0 时看跌翼 IV 本来就高于看涨翼，按符号说是对的。
# 样本取自 2026-09-17 ~ 10-03 eod.log 的展望原句（tests/test_state_direction.py）。
_RR_HIGH = ("rr25", lambda p: p >= 75)
_CONFLICTS = (
    (re.compile(
        rf"(?:put|看跌|下翼|左翼){_NC}{{0,8}}?(?:极贵|最贵|很贵|昂贵|极端溢价)"
        rf"|(?:RR|rr25|风险反转){_NC}{{0,10}}?(?:高分位|P(?:[89]\d|100)){_NC}{{0,4}}?"
        rf"(?:说|=|即|意味着?|代表)\s*(?:put|看跌|下行){_NC}{{0,4}}贵"
        rf"|(?:最贵|极贵|很贵)的(?:下行翼|下行保护|下翼|左翼|put|看跌)"
        rf"|极度偏\s*(?:put|看跌)"
        rf"|(?:抢购|在买|买入)(?:下跌保险|下行保护|下行翼|左翼|下翼|put|看跌)"
        rf"|保护{_NC}{{0,4}}抢筹"
        rf"|避险(?:定价|需求)?{_NC}{{0,2}}(?:到顶|拉满|极贵|最贵|很贵)"
        rf"|(?:下行|看跌)偏斜{_NC}{{0,12}}?(?:高位|到顶|一年顶|最陡|极端)"
        rf"|(?:下行|看跌)偏斜{_NC}{{0,8}}?P(?:9\d|100)"
        rf"|(?:保险|担忧|恐慌|对冲|溢价){_NC}{{0,10}}(?:全押|押满|全压|挤|集中)"
        rf"{_NC}{{0,6}}(?:下翼|左翼|左边|左侧|put)",
        re.I), (_RR_HIGH,)),
    # 「保险贵」也可能在说下尾概率；下尾概率也不高时才算反例
    (re.compile(rf"保险(?:定价)?(?:很|极|偏|太)?(?:贵|到顶)|保险{_NC}{{0,4}}天花板"),
     (_RR_HIGH, ("tail_p_down", lambda p: p <= 50))),
    (re.compile(rf"(?:put|看跌){_NC}{{0,8}}?(?:极便宜|最便宜|很便宜|廉价)", re.I),
     (("rr25", lambda p: p <= 25),)),
    (re.compile(rf"极度左偏|左偏{_NC}{{0,8}}?(?:极端|最重|最深|拉满|到顶|一年顶|年内高位|一年高位)"),
     (("bowley_skew", lambda p: p >= 75),)),
    (re.compile(rf"极度右偏|左偏{_NC}{{0,8}}?(?:最轻|最浅)"),
     (("bowley_skew", lambda p: p <= 25),)),
)


def _direction_table(packs: dict) -> dict:
    """{symbol: {indicator: (pct, reading)}}；各包 benchmark 里的指数也收进来（个股助手会提到它）。"""
    tab = {sym: {s["indicator"]: (s.get("pct"), s.get("reading")) for s in p.get("state") or []}
           for sym, p in packs.items()}
    for p in packs.values():
        b = p.get("benchmark") or {}
        if b.get("symbol") and b["symbol"] not in tab:
            pct, rd = b.get("state_pct") or {}, b.get("state_reading") or {}
            tab[b["symbol"]] = {k: (pct.get(k), rd.get(k)) for k in pct}
    return tab


def direction_conflicts(text: str, packs: dict) -> list[str]:
    """生成文本里与上下文方向白话相反的说法（关键词级反例检查）。

    按子句扫：子句点名的标的（「宽基 / 指数 / 大盘」= SPY、QQQ）即归属；没点名的沿用
    最近一次点名，泛指「个股 / 成分股」的子句不归属、也打断沿用。归属标的的分位**全部**
    落在反向区间才算命中——一句话同时说个股和指数时不误伤。返回可直接塞进纠错提示的描述串。
    """
    tab = _direction_table(packs)
    names = {s: re.compile(rf"(?<![A-Za-z]){re.escape(s)}(?![A-Za-z])") for s in tab}
    hits, last = [], []
    for clause in _CLAUSE.split(text):
        named = [s for s, rx in names.items() if rx.search(clause)]
        if _INDEX_WORDS.search(clause):
            named += [s for s in _INDEX_SYMBOLS if s in tab and s not in named]
        if not named and _STOCK_WORDS.search(clause):
            last = []
            continue
        targets = named or last
        last = named or last
        for rx, conds in _CONFLICTS:
            if not targets or not rx.search(clause):
                continue
            rows = [(s, ind, test, *tab[s].get(ind, (None, None)))
                    for s in targets for ind, test in conds]
            if all(p is not None and test(p) for _, _, test, p, _ in rows):
                why = "；".join(f"{s} {q.LABELS.get(ind, ind)} P{p:.0f} = {r or ''}"
                               for s, ind, _, p, r in rows)
                hits.append(f"『{clause.strip()}』与上下文方向相反——{why}")
                break   # 一个子句报一次
    return hits


def generate_checked(msg: dict, packs: dict, **kw) -> str:
    """generate + 方向反例自检：命中 → 带纠错重写一次；重写仍命中 → 末尾挂警告照发（不吞推送）。"""
    text = generate(msg["system"], msg["user"], **kw)
    hits = direction_conflicts(text, packs)
    if not hits:
        return text
    print(f"[方向自检] 首版命中 {len(hits)} 处，带纠错重写一次：", *hits, sep="\n  ")
    retry = (msg["user"] + "\n\n【方向纠错】上一版以下表述与上下文 reading 的方向相反：\n"
             + "\n".join(f"- {h}" for h in hits)
             + "\n方向一律以 reading 为准，重写全文，其余要求不变。")
    try:
        text = generate(msg["system"], retry, **kw)
    except AssistantError as e:
        # 重写失败（超时 / 中转故障）不能连首版一起丢：退回首版，挂首版命中的警告。
        print(f"[方向自检] 重写失败，退回首版挂警告发出：{e}")
    else:
        hits = direction_conflicts(text, packs)
    if hits:
        print(f"[方向自检] 发出版仍命中 {len(hits)} 处，挂警告：", *hits, sep="\n  ")
        text += ("\n\n⚠ 方向自检未通过——以下表述与数据方向相反，以面板为准：\n"
                 + "\n".join(f"· {h}" for h in hits))
    return text


def load_holdings() -> dict:
    """币安实际持仓快照（holdings_sync.entry_dates，即标的页 binance_entry 的来源）。

    返回 {"entries": {ticker: {...}}, "error": None}；fund.db 缺失或读取失败 →
    entries=None + error，与「确实无持仓」分开（读不到不能写成无持仓）。
    多标的装配只调一次，经 build_context(holdings=...) 共用。
    """
    from rnd import holdings_sync
    path = Path(holdings_sync.FUND_DB_PATH)
    if not path.exists():
        return {"entries": None, "error": f"币安持仓源 fund.db 不可用（{path}）"}
    c = q.conn()
    try:
        return {"entries": holdings_sync.entry_dates(c, path), "error": None}
    except Exception as e:  # noqa: BLE001  持仓读失败不该拖垮整份展望
        return {"entries": None, "error": f"读取币安持仓失败（{type(e).__name__}: {e}）"}
    finally:
        c.close()


def build_context(symbol: str, asof: str | None = None, *, holdings: dict | None = None) -> dict:
    """holdings：load_holdings() 的结果；None → 本函数自读。"""
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
    if positions:
        position = _position(ind, positions, current_close)
    else:
        position = _binance_position(c, symbol, asof, ind,
                                     holdings if holdings is not None else load_holdings(),
                                     current_close)
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
        "position": position,
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


def _deg(pct: float, hi: str, lo: str) -> str:
    """分位 → 程度短语；hi / lo 是分位高端 / 低端对应的形容词。"""
    if pct >= 90:
        return f"处于一年最{hi}之列"
    if pct >= 75:
        return f"比一年中多数日子{hi}"
    if pct > 25:
        return "处于一年常态区间"
    if pct > 10:
        return f"比一年中多数日子{lo}"
    return f"处于一年最{lo}之列"


# 方向单调的指标：主语 + 分位高端 / 低端的形容词
_PLAIN_DIRECTION = {
    "atm_iv": ("隐含波动率（期权整体价格）", "高", "低"),
    "ex_kurt": ("尾部肥度（超额峰度）", "高", "低"),
    "bf25": ("两翼相对 ATM 的溢价（smile 曲率）", "高", "低"),
    "term_slope": ("近月相对次月的 IV 溢价（事件压力）", "高", "低"),
    "tail_p_down": ("跌破 F·(1−x) 的 RN 概率", "高", "低"),
    "tail_p_up": ("涨破 F·(1+x) 的 RN 概率", "高", "低"),
}


def direction_reading(indicator: str, value, pct, dpct=None) -> str:
    """把 (原始值, 分位, 日环比分位Δ) 翻成方向白话，喂给模型照抄，不让它从分位自己推方向。

    分位对原始值算（rnd/state.py），分位高 = 原始值大。负值指标（指数 rr25、偏度）在这里
    最容易读反：SPY rr25 = −0.022 / P99 是「看跌保护相对看涨一年最便宜」，不是「put 贵」；
    日环比同理，rr25 Δ+11.6 是「看跌保护在变便宜」，不是「put 翼溢价在堆」。
    value 为 None 时（如事件只带分位）只给分位方向、不给符号句；|Δ|<1 不写日环比。
    """
    if pct is None:
        return "无分位（样本<60、闸门未过或当日无读数）：不陈述分位方向"
    out = _level_reading(indicator, value, pct)
    if dpct is not None and abs(dpct) >= 1:
        out += f"；日环比 {dpct:+.1f} 分位：{_delta_reading(indicator, value, dpct > 0)}"
    return out


def _delta_reading(indicator: str, value, up: bool) -> str:
    if indicator == "rr25":
        return f"看跌保护相对看涨在变{'便宜' if up else '贵'}"
    if indicator in ("skew", "log_skew", "bowley_skew"):
        if value is not None and value < 0:
            return "左偏在减轻" if up else "左偏在加深"
        if value is not None and value > 0:
            return "右偏在加重" if up else "右偏在减轻"
        return "偏度在走高（往右偏走）" if up else "偏度在走低（往左偏走）"
    return f"{_PLAIN_DIRECTION[indicator][0]}在走{'高' if up else '低'}"


def _level_reading(indicator: str, value, pct: float) -> str:
    mid = 25 < pct < 75
    if indicator == "rr25":
        core = f"看跌保护相对看涨{_deg(pct, '便宜', '贵')}"
        sign = gloss = None
        if value is not None and value < 0:
            sign = f"rr25<0：看跌翼 IV {'仍' if pct >= 75 else ''}高于看涨翼"
            gloss = f"看跌偏斜{_deg(pct, '平', '陡')}"
        elif value is not None and value > 0:
            sign = "rr25>0：看涨翼 IV 高于看跌翼"
            gloss = f"看涨溢价{_deg(pct, '高', '低')}"
        out = core if mid or gloss is None else f"{core}（{gloss}）"
        return f"{sign}；{out}" if sign else out
    if indicator in ("skew", "log_skew", "bowley_skew"):
        sign = None
        if mid:
            core = "偏度处于一年常态区间"
        elif value is not None and value < 0:
            core = f"左偏程度{_deg(pct, '轻', '重')}"
        elif value is not None and value > 0:
            core = f"右偏程度{_deg(pct, '重', '轻')}"
        else:
            core = f"偏度{_deg(pct, '高', '低')}"
        if value is not None and value != 0:
            sign = f"{indicator}{'<' if value < 0 else '>'}0：分布{'左' if value < 0 else '右'}偏"
        if not mid:
            core += f"（相对一年历史更{'右' if pct >= 75 else '左'}偏）"
        return f"{sign}；{core}" if sign else core
    subject, hi, lo = _PLAIN_DIRECTION[indicator]
    core = f"{subject}{_deg(pct, hi, lo)}"
    if indicator == "term_slope" and value is not None and value != 0:
        return f"{'近月 IV 高于次月（倒挂）' if value > 0 else '近月 IV 低于次月（正向结构）'}；{core}"
    return core


def _state(states: list[dict]) -> list[dict]:
    """10 个状态指标 × value/pct/dpct/sample_n + saying/label + 方向定义/方向白话，按 STATE_INDICATORS 排序。"""
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
            "definition": q.DIRECTION.get(ind_name, ""),
            "reading": direction_reading(ind_name, s.get("value"), s.get("pct"), s.get("dpct")),
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
    by_ind = {s["indicator"]: s for s in bench_states}
    pct = {k: by_ind.get(k, {}).get("pct") for k in STATE_INDICATORS}
    return {
        "symbol": bench,
        "state_pct": pct,
        "state_reading": {k: direction_reading(k, by_ind.get(k, {}).get("value"), pct[k],
                                               by_ind.get(k, {}).get("dpct"))
                          for k in STATE_INDICATORS},
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
        "source": "journal",
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


def _binance_position(c, symbol: str, asof: str | None, ind: dict | None,
                      holdings: dict, current_close) -> dict:
    """币安实际持仓（journal 无开仓时；2026-08-06 口径决策：以 fund 同步为准）。

    - 身份未登记 → identity/stop_q 为 None，stop_lines 同时给投机 Q25 与信念 Q05。
    - 冻结 = 本周期冻结（queries.cycle_freeze_date），与止盈止损页曲线一同一套数；
      标的页持仓卡 / 今日页的灯按入场日冻结，数字会不同。
    - 偏移与失效判定同 journal 轨：(现Q − 冻结Q)/冻结σ1；收盘确认，多头 close < 线 → 失效（空头反向）。
    """
    if holdings["entries"] is None:
        return {"open": None, "source": "binance",
                "reason": f"{holdings['error']}；持仓状态未知，不得写成无持仓"}
    e = holdings["entries"].get(symbol)
    if e is None:
        return {"open": False}
    # 用 snap 后的 rnd_date 比，不用 open_date：收盘后、cron 前开的仓 UTC 日已跨到次日，
    # 但对最新数据日仍算在仓（同止盈止损页）。只有历史 asof 早于开仓那个交易日才算不在仓。
    if asof and e.get("rnd_date") and e["rnd_date"] > asof:
        return {"open": False, "note": f"币安当前持仓开于 {e['open_date']}，晚于数据日 {asof}"}
    long = e["qty"] >= 0
    out = {
        "open": True,
        "source": "binance",
        "direction": "long" if long else "short",
        "identity": None,
        "stop_q": None,
        "identity_note": IDENTITY_NOTE,
        "entry_price": e["entry_price"],
        "qty": e["qty"],
        "open_date": e["open_date"],
        "current_close": current_close,
    }
    fd = cycle_start = None
    if ind is not None:
        fd, cycle_start = q.cycle_freeze_date(c, symbol, ind["expiry"], e.get("rnd_date"))
    fr = q._row(
        c, "SELECT date, expiry, forward, sigma1_abs, gate_pass, q05, q25, q50, q75, q95"
           " FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
        (symbol, fd)) if fd else None
    if fr is None:
        return out | {"frozen": None, "frozen_basis": "无可用冻结行（数据日无钉住指标）",
                      "offset": None, "stop_lines": None}

    sig = fr["sigma1_abs"]

    def line(qk: str) -> dict:
        level = fr[qk]
        inv = None
        if level is not None and current_close is not None:
            inv = (current_close < level) if long else (current_close > level)
        return {"quantile": qk, "level": level, "invalidated": inv}

    start = ("本周期第一天，入场早于本周期" if fd == cycle_start else "入场日在本周期内")
    gate = "" if fr["gate_pass"] else "；冻结日闸门未过"
    return out | {
        "frozen_basis": f"本周期冻结 {fd}（当前钉住到期 {fr['expiry']}；{start}；换月时重钉{gate}）",
        "frozen": {
            "frozen_date": fr["date"], "frozen_expiry": fr["expiry"],
            "frozen_forward": fr["forward"], "frozen_sigma1": sig,
            "frozen_gate_pass": bool(fr["gate_pass"]),
            **{f"frozen_{k}": fr[k] for k in _QUANTS}},
        "offset": {k: (ind[k] - fr[k]) / sig for k in _QUANTS} if sig else None,
        "stop_lines": {"speculative": line("q25"), "conviction": line("q05")},
    }


def _events(symbol: str, asof: str | None) -> list[dict]:
    """近窗事件，与仪表盘 events 同源口径（framework §3.3）；分位越线事件补方向白话。"""
    if not asof:
        return []
    evs = q.events(symbol, days=90).get("events", [])
    for e in evs:
        if e.get("kind") == "extreme" and e.get("indicator"):
            e["reading"] = direction_reading(e["indicator"], None, e.get("pct"))
    return evs


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

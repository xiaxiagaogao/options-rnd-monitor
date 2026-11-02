"""时间点上下文（O1）：给机器调用方按 asof 读 RND 行情侧数据 + 标的身份。

设计要点：
- 只读：本模块只发 SELECT；不写库、不调 LLM。
- 不读持仓：context 走 assistant.build_context(include_position=False)，不碰
  load_holdings / _binance_position / journal 未平仓。
- distribution 与 Q05/Q25/Q75/Q95 失效线是同一条曲线：rnd_indicators 的 pinned 行
  （rnd/timeseries.py 选最接近 30 DTE 的到期）对应的 rnd_curve 网格。分位数由
  rnd/compute.py 在这条曲线的 cdf 上用 rnd.quantile 算出（rnd/pipeline/bl.py）。
"""
import datetime as dt
import hashlib
import json
import math
import re

from rnd import identity as ident

from . import assistant, queries as q

ASOF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BadAsof(ValueError):
    """asof 不是合法的 YYYY-MM-DD 日期。"""


def parse_asof(asof: str) -> str:
    """严格校验 YYYY-MM-DD（不接受 2026-8-5 / 2026-02-30 / 带时间的串）。"""
    if not isinstance(asof, str) or not ASOF_RE.match(asof):
        raise BadAsof("asof 必须是 YYYY-MM-DD")
    try:
        d = dt.date.fromisoformat(asof)
    except ValueError as e:
        raise BadAsof("asof 必须是合法日期 YYYY-MM-DD") from e
    if d.isoformat() != asof:
        raise BadAsof("asof 必须是 YYYY-MM-DD")
    return asof


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _json_default(value):
    """把 numpy 标量等转成 JSON 原生类型；非有限浮点落成 null。"""
    if hasattr(value, "item"):
        value = value.item()
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    raise TypeError(f"无法序列化 {type(value).__name__}")


def canonical_json(payload: dict) -> str:
    """规范化 JSON：键排序、无空格、浮点用 Python repr（json.dumps 默认）。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=_json_default)


def context_hash(payload: dict) -> str:
    """对响应内容做 sha256:不含 context_hash 自身和 generated_at,同样的内容任何时候请求哈希都相同。"""
    body = {k: v for k, v in payload.items() if k not in ("context_hash", "generated_at")}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def _jsonable(value):
    """深拷贝成纯 JSON 类型，保证 FastAPI 响应与 hash 用的是同一份数。"""
    return json.loads(canonical_json(value))


def _distribution(c, symbol: str, date: str | None) -> dict | None:
    """pinned 分布 → API 字段；strikes 升序、cdf 等长。"""
    if not date:
        return None
    dist = q.pinned_distribution(c, symbol, date)
    if dist is None:
        return None
    pairs = sorted(zip((float(x) for x in dist["strikes"]),
                       (float(x) for x in dist["cdf"])), key=lambda p: p[0])
    return {
        "asof": date,
        "expiry": dist["expiry"],
        "forward": float(dist["forward"]),
        "strikes": [p[0] for p in pairs],
        "cdf": [p[1] for p in pairs],
    }


def build_payload(symbol: str, asof: str | None = None, *,
                  generated_at: str | None = None) -> dict:
    """组装 O1 响应。symbol 由路由保证已在池中；asof 省略 = 最新 RND 日。"""
    symbol = symbol.upper()
    requested = parse_asof(asof) if asof is not None else None
    c = q.conn()
    try:
        actual = (q.latest_date(c, symbol, requested) if requested is not None
                  else q.latest_date(c, symbol))
        ind = (q._row(c, "SELECT * FROM rnd_indicators"
                         " WHERE symbol=? AND date=? AND pinned=1",
                      (symbol, actual)) if actual else None)
        distribution = _distribution(c, symbol, actual)
        asof_out = requested if requested is not None else actual
        identity = ident.as_of(c, symbol, asof_out) if asof_out else None
    finally:
        c.close()

    # 显式 asof 早于第一条 RND 时 actual 为 None：把 requested 传下去，build_context
    # 会收敛到 ≤ requested 的最近日（仍为 None），不会退回「最新」读未来。
    ctx_asof = actual if actual is not None else asof_out
    context = assistant.build_context(symbol, ctx_asof, include_position=False)

    payload = {
        "symbol": symbol,
        "asof": asof_out,
        "date": actual,
        "gate_pass": (None if ind is None or ind["gate_pass"] is None
                      else bool(ind["gate_pass"])),
        "distribution": distribution,
        "identity": identity,
        "context": context,
        "generated_at": generated_at or _now_iso(),
    }
    payload = _jsonable(payload)
    payload["context_hash"] = context_hash(payload)
    return payload

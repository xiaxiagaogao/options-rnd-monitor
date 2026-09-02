"""期权异动检测（research-assistant-framework §2.3 推送面）。

口径（用户定 2026-07-22）：
  1. 状态分位「穿越」P90/P10——今日 ≥90（或 ≤10）且昨日未越线（或昨日无读数）。
     只报新穿越，避免同一极端天天重复推。
  2. 双峰亮灯——今日 n_modes≥2。
  3. 持仓偏移 >1σ——有开仓时，任一分位 (现Q−冻结Q)/冻结σ1 绝对值 >1。
不含闸门 FAIL、不含 roll（用户明确不要）。纯只读，复用 queries。
"""
import json

from . import queries as q

_QUANTS = ("q05", "q25", "q50", "q75", "q95")


def _two_latest(c, symbol: str) -> tuple[str | None, str | None]:
    rows = c.execute(
        "SELECT date FROM rnd_indicators WHERE symbol=? AND pinned=1 ORDER BY date DESC LIMIT 2",
        (symbol,)).fetchall()
    d = rows[0][0] if rows else None
    prev = rows[1][0] if len(rows) > 1 else None
    return d, prev


def todays_anomalies(symbols: list[str] | None = None) -> list[dict]:
    c = q.conn()
    syms = symbols or q.get_symbols()
    items: list[dict] = []
    for sym in syms:
        d, prev = _two_latest(c, sym)
        if not d:
            continue
        ind = q._row(c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
                     (sym, d))
        if ind is None:
            continue

        # 1. 双峰
        if (ind["n_modes"] or 0) >= 2:
            peaks = json.loads(ind["modes_json"] or "[]")
            pk = " / ".join(f"{p['strike']:.0f}（{p['mass']:.0%}）" for p in peaks[:2])
            items.append({"symbol": sym, "date": d, "kind": "bimodal",
                          "text": f"{sym} 双峰亮灯：{pk}"})

        # 2. 分位穿越 P90/P10（今日越线且昨日未越）
        today = {s["indicator"]: s["pct"] for s in q.state_at(c, sym, d)}
        prevm = {s["indicator"]: s["pct"] for s in q.state_at(c, sym, prev)} if prev else {}
        for ind_name, pct in today.items():
            if pct is None:
                continue
            pp = prevm.get(ind_name)
            label = q.LABELS.get(ind_name, ind_name)
            if pct >= 90 and (pp is None or pp < 90):
                items.append({"symbol": sym, "date": d, "kind": "extreme",
                              "text": f"{sym} {label} 升至 P{pct:.0f}"})
            elif pct <= 10 and (pp is None or pp > 10):
                items.append({"symbol": sym, "date": d, "kind": "extreme",
                              "text": f"{sym} {label} 降至 P{pct:.0f}"})

        # 3. 持仓偏移 >1σ
        pos = q.open_positions(c, sym)
        if pos:
            p = pos[0]
            sig = p.get("frozen_sigma1")
            if sig:
                for k in _QUANTS:
                    off = (ind[k] - p[f"frozen_{k}"]) / sig
                    if abs(off) > 1:
                        items.append({"symbol": sym, "date": d, "kind": "offset",
                                      "text": f"{sym} 持仓 {k.upper()} 偏移 {off:+.2f}σ（>1σ，加减仓信息）"})
    c.close()
    return items


def stale_symbols(symbols: list[str], vendor_latest: str) -> list[tuple[str, str | None]]:
    """库内最新数据日落后数据源的标的 → [(symbol, 库内最新数据日 or None)]。

    基准取 ThetaData 已出 EOD 的最近交易日（`fetch.latest_trading_day`），不自维护
    交易日历/假期表：周末跑时基准就是周五，与库内相等即不算陈旧。
    """
    c = q.conn()
    out = [(sym, d) for sym in symbols
           if (d := q.latest_date(c, sym)) is None or d < vendor_latest]
    c.close()
    return out


def format_staleness(stale: list[tuple[str, str | None]], vendor_latest: str,
                     n_total: int) -> str:
    """陈旧告警推送体（2026-09-01 事故：拉取全挂而推送照常发旧报告，静默两天）。"""
    lines = "\n".join(f"· {sym} 停在 {d}" if d else f"· {sym} 无数据" for sym, d in stale)
    tail = ("今日不推异动与市场展望——旧数据不当新数据发。\n去 VPS 看 eod.log 排查。"
            if len(stale) >= n_total else
            "以上标的今日读数缺失；其余标的照常推送。")
    return (f"⚠ RND 数据陈旧 · 未更新\n{'—' * 20}\n"
            f"数据源最近交易日：{vendor_latest}\n"
            f"落后标的 {len(stale)}/{n_total}：\n{lines}\n\n{tail}")


def format_alerts(items: list[dict], asof: str | None = None) -> str:
    """异动项 → TG 纯文本推送体。无异动返回空串（调用方据此决定不推）。"""
    if not items:
        return ""
    head = f"⚠ 期权异动 · {asof or items[0]['date']}"
    glyph = {"extreme": "▲", "bimodal": "◆", "offset": "●"}
    body = "\n".join(f"{glyph.get(it['kind'], '·')} {it['text']}" for it in items)
    return (f"{head}\n{'—' * 20}\n{body}\n\n"
            f"（RN≠真实概率 · 状态异动，非交易信号 · 数据描述非建议）")

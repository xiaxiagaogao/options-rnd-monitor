"""仪表盘只读查询层：五表 → 前端 JSON。派生逻辑尽量下沉到这里，路由保持薄。"""
import datetime as dt
import json
import uuid
from functools import lru_cache

import numpy as np
import pandas as pd
import yaml

from rnd import db
from rnd.config import PROJECT_ROOT
from rnd.state import STATE_INDICATORS

# 指标白话（spec §4 说明列的 UI 化）
SAYINGS = {
    "atm_iv": "等价 IV rank——高分位防 vol crush，低分位适合埋伏",
    "rr25": "put/call 翼相对价——独立口径情绪计",
    "skew": "分布本身的不对称（含对数正态基线）",
    "log_skew": "纯 smile 驱动口径，与 RR 互为校验",
    "bowley_skew": "分位数口径的稳健版偏度——翼部噪声免疫",
    "ex_kurt": "四矩里最脆，看闸门脸色用",
    "bf25": "smile 曲率 = 峰度的独立口径校验",
    "term_slope": "倒挂进高分位 = 事件压力计",
    "tail_p_down": "下跌保险的市场定价（RN 口径，偏肥）",
    "tail_p_up": "右尾抢筹的定价",
}
LABELS = {
    "atm_iv": "ATM IV", "rr25": "25Δ 风险反转", "skew": "偏度（价格）",
    "log_skew": "偏度（对数）", "bowley_skew": "偏度（Bowley）", "ex_kurt": "超额峰度",
    "bf25": "25Δ 蝶式", "term_slope": "期限结构斜率",
    "tail_p_down": "下尾概率", "tail_p_up": "上尾概率",
}


def get_symbols() -> list[str]:
    cfg = yaml.safe_load((PROJECT_ROOT / "symbols.yaml").read_text())
    return list(cfg.get("fixed", [])) + list(cfg.get("dynamic") or [])


def benchmark_of(symbol: str) -> str:
    return "SPY" if symbol == "QQQ" else "QQQ"


def conn():
    return db.get_conn()


def latest_date(c, symbol: str) -> str | None:
    r = c.execute("SELECT MAX(date) FROM rnd_indicators WHERE symbol=? AND pinned=1",
                  (symbol,)).fetchone()
    return r[0] if r else None


def _row(c, q, args=()):
    cur = c.execute(q, args)
    r = cur.fetchone()
    if r is None:
        return None
    return dict(zip([d[0] for d in cur.description], r))


def open_positions(c, symbol: str | None = None):
    """开仓中的持仓 = 每个 position_id 的最新 open/roll_repin 事件，且无 close 事件。"""
    q = """
      SELECT t.* FROM trade_journal t
      JOIN (SELECT position_id, MAX(event_id) AS mid FROM trade_journal
            WHERE event_type IN ('open','roll_repin') GROUP BY position_id) last
        ON last.mid = t.event_id
      WHERE t.position_id NOT IN
        (SELECT position_id FROM trade_journal WHERE event_type='close')
    """
    cur = c.execute(q)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if symbol:
        rows = [r for r in rows if r["symbol"] == symbol]
    return rows


def state_at(c, symbol: str, date: str) -> list[dict]:
    cur = c.execute(
        "SELECT indicator, value, pct, dpct, sample_n FROM rnd_state"
        " WHERE symbol=? AND date=?", (symbol, date))
    return [dict(zip(["indicator", "value", "pct", "dpct", "sample_n"], r))
            for r in cur.fetchall()]


def overview() -> dict:
    c = conn()
    out = []
    for sym in get_symbols():
        d = latest_date(c, sym)
        if d is None:
            out.append({"symbol": sym, "ready": False})
            continue
        ind = _row(c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
                   (sym, d))
        states = state_at(c, sym, d)
        extremes = [
            {"indicator": s["indicator"], "label": LABELS.get(s["indicator"], s["indicator"]),
             "pct": s["pct"], "side": "high" if s["pct"] >= 90 else "low"}
            for s in states if s["pct"] is not None and (s["pct"] >= 90 or s["pct"] <= 10)
        ]
        pos = open_positions(c, sym)
        out.append({
            "symbol": sym, "ready": True, "date": d,
            "gate_pass": bool(ind["gate_pass"]), "n_modes": ind["n_modes"],
            "atm_iv_pct": next((s["pct"] for s in states if s["indicator"] == "atm_iv"), None),
            "extremes": extremes, "positions": len(pos),
        })
    c.close()
    return {"symbols": out}


def symbol_detail(symbol: str) -> dict:
    c = conn()
    d = latest_date(c, symbol)
    ind = _row(c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
               (symbol, d))
    expiries = [
        _row(c, "SELECT expiry, dte, pinned, gate_pass, atm_iv FROM rnd_indicators"
                " WHERE symbol=? AND date=? AND expiry=?", (symbol, d, e[0]))
        for e in c.execute("SELECT expiry FROM rnd_indicators WHERE symbol=? AND date=?"
                           " ORDER BY dte", (symbol, d)).fetchall()
    ]
    bench = benchmark_of(symbol)
    bench_states = {s["indicator"]: s for s in state_at(c, bench, d)}
    states = []
    for s in state_at(c, symbol, d):
        b = bench_states.get(s["indicator"], {})
        states.append(s | {
            "label": LABELS.get(s["indicator"], s["indicator"]),
            "saying": SAYINGS.get(s["indicator"], ""),
            "bench_pct": b.get("pct"),
        })
    order = {k: i for i, k in enumerate(STATE_INDICATORS)}
    states.sort(key=lambda s: order.get(s["indicator"], 99))
    gate_detail = json.loads(ind["gate_detail"]) if ind.get("gate_detail") else {}
    modes = json.loads(ind["modes_json"]) if ind.get("modes_json") else []
    pos = open_positions(c, symbol)
    c.close()
    return {
        "symbol": symbol, "date": d, "benchmark": bench,
        "indicators": ind, "gate_detail": gate_detail, "modes": modes,
        "expiries": expiries, "states": states, "positions": pos,
    }


def fan(symbol: str, days: int = 120) -> dict:
    c = conn()
    df = pd.read_sql_query(
        "SELECT date, expiry, forward, q05, q25, q50, q75, q95, roll, gate_pass, n_modes,"
        " tail_p_down, tail_p_up"
        " FROM rnd_indicators WHERE symbol=? AND pinned=1 ORDER BY date DESC LIMIT ?",
        c, params=(symbol, days))
    df = df.iloc[::-1].reset_index(drop=True)
    closes = pd.read_sql_query(
        "SELECT DISTINCT date, underlying_close FROM raw_chain WHERE symbol=?"
        " AND date >= ? ORDER BY date", c, params=(symbol, df["date"].iloc[0] if len(df) else ""))
    frozen = [
        {"position_id": p["position_id"], "date": p["event_date"],
         "stop_q": p["stop_q"], "level": p[f"frozen_{p['stop_q']}"],
         "entry": p["entry_price"], "direction": p["direction"]}
        for p in open_positions(c, symbol) if p.get("stop_q")
    ]
    c.close()
    return {
        "dates": df["date"].tolist(),
        "close": dict(zip(closes["date"], closes["underlying_close"].astype(float))),
        "q": {k: df[k].tolist() for k in ("q05", "q25", "q50", "q75", "q95")},
        "forward": df["forward"].tolist(),
        "roll_dates": df.loc[df["roll"] == 1, "date"].tolist(),
        "gate_fail_dates": df.loc[df["gate_pass"] == 0, "date"].tolist(),
        "bimodal_dates": df.loc[df["n_modes"] >= 2, "date"].tolist(),
        "tails": {"down": df["tail_p_down"].tolist(), "up": df["tail_p_up"].tolist()},
        "frozen": frozen,
    }


def density(symbol: str, date: str | None = None, expiry: str | None = None) -> dict:
    c = conn()
    d = date or latest_date(c, symbol)
    if expiry is None:
        r = c.execute("SELECT expiry FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
                      (symbol, d)).fetchone()
        expiry = r[0] if r else None
    row = _row(c, "SELECT grid_json, fit_meta FROM rnd_curve"
                  " WHERE symbol=? AND date=? AND expiry=?", (symbol, d, expiry))
    ind = _row(c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND expiry=?",
               (symbol, d, expiry))
    c.close()
    if not row or not ind:
        return {"ok": False, "date": d, "expiry": expiry}
    grid = json.loads(row["grid_json"])
    meta = json.loads(row["fit_meta"])
    F = ind["forward"]
    x_lo, x_hi = meta.get("x_quoted_range", [None, None])
    return {
        "ok": True, "date": d, "expiry": expiry, "forward": F,
        "strikes": grid["strikes"], "density": grid["density"], "cdf": grid["cdf"],
        "k_quoted": [F * float(np.exp(x_lo)), F * float(np.exp(x_hi))] if x_lo is not None else None,
        "quantiles": {k: ind[k] for k in ("q05", "q25", "q50", "q75", "q95")},
        "in_range": {k: bool(ind[f"{k}_in_range"]) for k in ("q05", "q25", "q50", "q75", "q95")},
        "fit_meta": {k: meta.get(k) for k in ("smoothing_s", "s_multiplier", "fit_rmse_iv",
                                              "n_strikes_used", "extrapolation", "wing_slopes")},
        "gate_pass": bool(ind["gate_pass"]),
    }


def _detect_price_splits(dates: list[str], close_by_date: dict, thr: float = 0.35) -> list[dict]:
    """从收盘价跳变识别拆合股。ratio = 新收盘/旧收盘（10:1 拆股 ≈ 0.1）。"""
    splits = []
    prev_c = None
    for d in dates:
        c = close_by_date.get(d)
        if c is None or c <= 0:
            continue
        if prev_c is not None and prev_c > 0:
            r = float(c) / float(prev_c)
            if r <= (1.0 - thr) or r >= (1.0 + thr):
                splits.append({
                    "date": d,
                    "ratio": r,
                    "from_close": float(prev_c),
                    "to_close": float(c),
                    "factor_label": _split_label(r),
                })
        prev_c = float(c)
    return splits


def _split_label(ratio: float) -> str:
    """把 0.1 / 10 等 ratio 格式化成 10:1 / 1:10。"""
    if ratio <= 0:
        return f"×{ratio:.3g}"
    if ratio < 1:
        n = round(1.0 / ratio)
        if abs(1.0 / ratio - n) < 0.08:
            return f"{n}:1 拆股"
    else:
        n = round(ratio)
        if abs(ratio - n) < 0.08:
            return f"1:{n} 合股"
    return f"×{ratio:.3g}"


def _split_factors(dates: list[str], splits: list[dict]) -> dict[str, float]:
    """每个交易日到「现股口径」的乘子：拆股日前的价格 × factor = 现股等价价。"""
    split_ratio = {s["date"]: s["ratio"] for s in splits}
    factors: dict[str, float] = {}
    running = 1.0
    for d in reversed(dates):
        factors[d] = running
        if d in split_ratio:
            running *= split_ratio[d]
    return factors


@lru_cache(maxsize=8)
def _heatmap_cached(symbol: str, latest: str) -> dict:
    """密度时序热力图。默认按现货跳变把历史复权到现股口径，避免 NVDA 10:1 等拆股把 y 轴撑爆。"""
    c = conn()
    rows = pd.read_sql_query(
        "SELECT i.date, c.grid_json FROM rnd_indicators i"
        " JOIN rnd_curve c USING (date, symbol, expiry)"
        " WHERE i.symbol=? AND i.pinned=1 ORDER BY i.date", c, params=(symbol,))
    closes = pd.read_sql_query(
        "SELECT DISTINCT date, underlying_close FROM raw_chain WHERE symbol=? ORDER BY date",
        c, params=(symbol,))
    c.close()
    if rows.empty:
        return {"dates": [], "prices": [], "cells": [], "close": {}, "splits": [], "adjusted": False}

    dates = rows["date"].tolist()
    close_raw = dict(zip(closes["date"], closes["underlying_close"].astype(float)))
    # 只在热力图日期轴上侦测跳变，避免非交易日/缺链干扰
    close_on_axis = {d: close_raw[d] for d in dates if d in close_raw}
    splits = _detect_price_splits(dates, close_on_axis)
    factors = _split_factors(dates, splits)

    grids = [json.loads(g) for g in rows["grid_json"]]
    adj_strikes = []
    adj_density = []
    for d, g in zip(dates, grids):
        f = factors.get(d, 1.0)
        k = np.asarray(g["strikes"], dtype=float) * f
        dens = np.asarray(g["density"], dtype=float)
        # K' = f·K 时概率密度需 /f，保持 ∫p dk = 1
        if f != 0:
            dens = dens / f
        adj_strikes.append(k)
        adj_density.append(dens)

    lo = float(min(k[0] for k in adj_strikes))
    hi = float(max(k[-1] for k in adj_strikes))
    # 用复权收盘路径收一收极端翼，避免个别外推点把网格拉稀
    adj_closes = []
    for d in dates:
        c0 = close_raw.get(d)
        if c0 is not None:
            adj_closes.append(float(c0) * factors.get(d, 1.0))
    if adj_closes:
        c_lo, c_hi = min(adj_closes), max(adj_closes)
        pad = max((c_hi - c_lo) * 0.35, c_hi * 0.08, 5.0)
        lo = max(lo, c_lo - pad)
        hi = min(hi, c_hi + pad)
        if hi <= lo:
            lo, hi = c_lo * 0.7, c_hi * 1.3

    price_grid = np.linspace(lo, hi, 140)
    Z = np.zeros((len(price_grid), len(grids)))
    for j, (k, dens) in enumerate(zip(adj_strikes, adj_density)):
        Z[:, j] = np.interp(price_grid, k, dens, left=0.0, right=0.0)

    # ECharts heatmap 数据格式 [x_idx, y_idx, value]，density 做 0.4 次幂增强低值可见性
    Zp = np.power(np.clip(Z, 0, None), 0.4)
    zmax = float(Zp.max()) if Zp.size else 0.0
    if zmax > 0:
        Zp = Zp / zmax
    data = [[j, i, round(float(Zp[i, j]), 4)]
            for j in range(Z.shape[1]) for i in range(Z.shape[0]) if Zp[i, j] > 0.01]

    close_adj = {
        d: round(float(close_raw[d]) * factors.get(d, 1.0), 4)
        for d in close_raw
    }
    return {
        "dates": dates,
        "prices": [round(float(p), 1) for p in price_grid],
        "cells": data,
        "close": close_adj,
        "splits": splits,
        "adjusted": bool(splits),
    }


def heatmap(symbol: str) -> dict:
    c = conn()
    d = latest_date(c, symbol)
    c.close()
    return _heatmap_cached(symbol, d or "")


def events(symbol: str | None = None, days: int = 90) -> dict:
    c = conn()
    syms = [symbol] if symbol else get_symbols()
    evs = []
    for sym in syms:
        df = pd.read_sql_query(
            "SELECT date, expiry, roll, gate_pass, n_modes, modes_json FROM rnd_indicators"
            " WHERE symbol=? AND pinned=1 ORDER BY date DESC LIMIT ?",
            c, params=(sym, days))
        for _, r in df.iterrows():
            if r["roll"] == 1:
                evs.append({"date": r["date"], "symbol": sym, "kind": "roll",
                            "glyph": "●", "text": f"roll：改钉 {r['expiry']}"})
            if r["gate_pass"] == 0:
                evs.append({"date": r["date"], "symbol": sym, "kind": "gate",
                            "glyph": "▮", "text": "闸门未过 · 状态类信号静默"})
            if (r["n_modes"] or 0) >= 2:
                peaks = json.loads(r["modes_json"] or "[]")
                pk = " / ".join(f"{p['strike']:.0f}({p['mass']:.0%})" for p in peaks[:2])
                evs.append({"date": r["date"], "symbol": sym, "kind": "bimodal",
                            "glyph": "◆", "text": f"双峰：{pk}"})
        # 分位越阈（进入 ≥90 或 ≤10）
        st = pd.read_sql_query(
            "SELECT date, indicator, pct FROM rnd_state WHERE symbol=? AND pct IS NOT NULL"
            " ORDER BY date DESC LIMIT ?", c, params=(sym, days * len(STATE_INDICATORS)))
        st = st.iloc[::-1]
        for ind_name, g in st.groupby("indicator"):
            p = g["pct"].to_numpy()
            dts = g["date"].tolist()
            for i in range(1, len(p)):
                if p[i] >= 90 and p[i - 1] < 90:
                    evs.append({"date": dts[i], "symbol": sym, "kind": "extreme",
                                "glyph": "⚠", "pct": p[i],
                                "text": f"{LABELS.get(ind_name, ind_name)} 升至 P{p[i]:.0f}"})
                elif p[i] <= 10 and p[i - 1] > 10:
                    evs.append({"date": dts[i], "symbol": sym, "kind": "extreme",
                                "glyph": "⚠", "pct": p[i],
                                "text": f"{LABELS.get(ind_name, ind_name)} 降至 P{p[i]:.0f}"})
    evs.sort(key=lambda e: e["date"], reverse=True)
    c.close()
    return {"events": evs[:40]}


def journal_open(payload: dict) -> dict:
    """开仓事件：服务端负责冻结快照与 size 反解（§6 公式强制身份自洽）。"""
    c = conn()
    sym = payload["symbol"].upper()
    d = latest_date(c, sym)
    ind = _row(c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
               (sym, d))
    if ind is None:
        c.close()
        return {"ok": False, "error": f"{sym} 无可用指标行"}
    identity = payload.get("identity", "speculative")
    stop_q = "q05" if identity == "conviction" else "q25"
    entry = float(payload["entry_price"])
    budget = float(payload.get("risk_budget") or 0)
    stop_level = ind[stop_q]
    dist = abs(entry - stop_level)
    size = round(budget / dist, 2) if budget and dist > 0 else None
    target = payload.get("target_price")
    rationale = payload.get("target_rationale") or ""
    if target and float(target) > ind["q95"] and not rationale.strip():
        c.close()
        return {"ok": False,
                "error": "目标价高于冻结 Q95：需书面理由（你在下市场赔率<5% 的注，凭什么你对）"}
    pid = uuid.uuid4().hex[:8]
    c.execute(
        """INSERT INTO trade_journal
           (position_id, event_type, event_date, symbol, direction, identity, entry_price,
            frozen_expiry, frozen_q05, frozen_q25, frozen_q50, frozen_q75, frozen_q95,
            frozen_sigma1, frozen_forward, stop_q, risk_budget, size,
            target_price, target_rationale, notes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pid, "open", d, sym, payload.get("direction", "long"), identity, entry,
         ind["expiry"], ind["q05"], ind["q25"], ind["q50"], ind["q75"], ind["q95"],
         ind["sigma1_abs"], ind["forward"], stop_q, budget or None, size,
         float(target) if target else None, rationale or None,
         payload.get("notes") or None))
    c.commit()
    c.close()
    return {"ok": True, "position_id": pid, "frozen_stop": stop_level, "size": size,
            "snapshot_date": d}


def journal_close(payload: dict) -> dict:
    c = conn()
    pid = payload["position_id"]
    base = _row(c, "SELECT symbol FROM trade_journal WHERE position_id=? LIMIT 1", (pid,))
    if base is None:
        c.close()
        return {"ok": False, "error": "position_id 不存在"}
    d = latest_date(c, base["symbol"])
    c.execute(
        "INSERT INTO trade_journal (position_id, event_type, event_date, symbol,"
        " close_price, close_reason) VALUES (?,?,?,?,?,?)",
        (pid, "close", d, base["symbol"],
         float(payload.get("close_price") or 0) or None, payload.get("close_reason") or None))
    c.commit()
    c.close()
    return {"ok": True}


def journal_list() -> dict:
    c = conn()
    cur = c.execute("SELECT * FROM trade_journal ORDER BY event_id DESC LIMIT 100")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    c.close()
    return {"events": rows, "open": open_positions(conn())}


def dcdf(symbol: str) -> dict:
    """ΔCDF 质量迁移：最新两个 pinned 交易日的 CDF 差（今 − 昨）。

    正区 = 概率质量流向该行权价下方。跨 roll 日（到期不同）不可比，如实声明。"""
    c = conn()
    rows = pd.read_sql_query(
        "SELECT i.date, i.expiry, c.grid_json FROM rnd_indicators i"
        " JOIN rnd_curve c USING (date, symbol, expiry)"
        " WHERE i.symbol=? AND i.pinned=1 ORDER BY i.date DESC LIMIT 2",
        c, params=(symbol,))
    c.close()
    if len(rows) < 2:
        return {"ok": False}
    today, prev = json.loads(rows.iloc[0]["grid_json"]), json.loads(rows.iloc[1]["grid_json"])
    comparable = rows.iloc[0]["expiry"] == rows.iloc[1]["expiry"]
    lo = max(today["strikes"][0], prev["strikes"][0])
    hi = min(today["strikes"][-1], prev["strikes"][-1])
    grid = np.linspace(lo, hi, 300)
    cdf_t = np.interp(grid, today["strikes"], today["cdf"])
    cdf_p = np.interp(grid, prev["strikes"], prev["cdf"])
    d = cdf_t - cdf_p
    return {
        "ok": True, "comparable": bool(comparable),
        "dates": [rows.iloc[1]["date"], rows.iloc[0]["date"]],
        "expiries": [rows.iloc[1]["expiry"], rows.iloc[0]["expiry"]],
        "strikes": [round(float(k), 2) for k in grid],
        "dcdf": [round(float(v), 5) for v in d],
        "wasserstein": float(np.trapezoid(np.abs(d), grid)),
        "net_shift": float(-np.trapezoid(d, grid)),   # 正 = 分布整体上移
    }


@lru_cache(maxsize=8)
def _pit_cached(symbol: str, latest: str) -> dict:
    """PIT 校准：历史 pinned 分布的 CDF 代入到期日实际收盘。

    样本按日重叠（自相关），独立到期数另行报告；RN 含风险溢价，
    偏离均匀是预期而非缺陷——这张图量化的就是那个楔子（spec §6 解读铁律）。"""
    c = conn()
    rows = pd.read_sql_query(
        "SELECT i.date, i.expiry, c.grid_json FROM rnd_indicators i"
        " JOIN rnd_curve c USING (date, symbol, expiry)"
        " WHERE i.symbol=? AND i.pinned=1 AND i.expiry <= ? ORDER BY i.date",
        c, params=(symbol, latest))
    closes = pd.read_sql_query(
        "SELECT DISTINCT date, underlying_close FROM raw_chain WHERE symbol=? ORDER BY date",
        c, params=(symbol,))
    c.close()
    close_map = dict(zip(closes["date"], closes["underlying_close"].astype(float)))
    trading_days = sorted(close_map)

    def realized_at(expiry: str):
        # 到期日（或其前最近交易日）的收盘
        idx = [d for d in trading_days if d <= expiry]
        return close_map[idx[-1]] if idx else None

    realized = {e: realized_at(e) for e in rows["expiry"].unique()}
    pits = []
    for _, r in rows.iterrows():
        s_t = realized.get(r["expiry"])
        if s_t is None:
            continue
        g = json.loads(r["grid_json"])
        pits.append(float(np.interp(s_t, g["strikes"], g["cdf"])))
    if not pits:
        return {"ok": False}
    pits_arr = np.array(pits)
    hist, edges = np.histogram(pits_arr, bins=10, range=(0, 1))
    return {
        "ok": True,
        "n_samples": int(len(pits_arr)),
        "n_expiries": int(rows["expiry"].nunique()),
        "hist": hist.tolist(),
        "bin_edges": [round(float(e), 2) for e in edges],
        "breach_q05": float((pits_arr < 0.05).mean()),
        "breach_q25": float((pits_arr < 0.25).mean()),
        "above_q95": float((pits_arr > 0.95).mean()),
        "mean_pit": float(pits_arr.mean()),
    }


def pit(symbol: str) -> dict:
    c = conn()
    d = latest_date(c, symbol)
    c.close()
    return _pit_cached(symbol, d or "")


def diagnostics(symbol: str, date: str, expiry: str) -> dict:
    """Smile 拟合诊断：从 raw_chain 复算 IV 散点 + 拟合曲线（闸门申诉通道）。"""
    from rnd.compute import ComputeError, compute_day
    c = conn()
    chain = pd.read_sql_query(
        "SELECT strike, right, bid, ask, underlying_close, sofr FROM raw_chain"
        " WHERE date=? AND symbol=? AND expiry=?", c, params=(date, symbol, expiry))
    c.close()
    if chain.empty:
        return {"ok": False, "error": "raw_chain 无该日数据"}
    try:
        res = compute_day(chain[["strike", "right", "bid", "ask"]],
                          float(chain["underlying_close"].iloc[0]),
                          float(chain["sofr"].iloc[0]),
                          dt.date.fromisoformat(date), dt.date.fromisoformat(expiry),
                          symbol, 0.05 if symbol in ("SPY", "QQQ") else 0.10)
    except ComputeError as e:
        return {"ok": False, "error": str(e)}
    sm = res.rnd.smile
    xx = np.linspace(sm.x_min - 0.12, sm.x_max + 0.12, 200)
    return {
        "ok": True,
        "points": {"x": sm.x.tolist(), "iv": sm.iv.tolist()},
        "curve": {"x": xx.tolist(), "iv": np.asarray(sm(xx)).tolist()},
        "x_quoted": [sm.x_min, sm.x_max],
        "meta": {"rmse": sm.rmse, "s_multiplier": sm.s_multiplier,
                 "n": int(len(sm.x)), "wing_slopes": sm.fit_meta["wing_slopes"],
                 "checks": res.checks},
        "gate_detail": json.loads(res.indicators["gate_detail"]),
    }

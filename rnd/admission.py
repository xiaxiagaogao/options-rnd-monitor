"""动态槽准入检查器（spec §2 / §8.6）：换入前强制、自动执行的链质量体检。

两条硬指标（阈值为 spec 初版，经在池标的实测校准）：
  1. ATM 邻域（收盘价 ±5%）双边报价的相对点差中位数 ≤ 5%
  2. ±20% moneyness 内有双边报价的行权价数 ≥ 20

哲学同质量闸门：宁可没信号，不可有假信号——准入是入职体检，闸门是日检。
"""
import datetime as dt

import pandas as pd

from . import db, fetch

ATM_BAND = 0.05          # ATM 邻域：收盘价 ±5%
MONEYNESS_BAND = 0.20    # 覆盖检查带：±20%
# 阈值 2026-07-17 实测校准（spec §2 授权）：在池 SPY/QQQ/NVDA 点差 0.9–1.3%、
# 双边行权价 176/57/16；AMD 2.4%/20（spec：够格）、RKLB 4.1%/6（spec：太薄）。
# 初版 5%/20 会误杀 NVDA 级链（高价股行权价间距宽）且放过 RKLB 的点差。
MAX_ATM_SPREAD = 0.03    # 阈值 1（初版 0.05）
MIN_TWO_SIDED = 15       # 阈值 2（初版 20）


def _metrics_from_chain(chain: pd.DataFrame, close: float) -> dict:
    """chain 列：strike, right, bid, ask。"""
    df = chain.copy()
    df["two_sided"] = (df["bid"] > 0) & (df["ask"] > 0) & (df["bid"] <= df["ask"])
    df["mid"] = (df["bid"] + df["ask"]) / 2
    df["rel_spread"] = (df["ask"] - df["bid"]) / df["mid"].where(df["mid"] > 0)

    atm = df[df["two_sided"] & (df["strike"].sub(close).abs() <= close * ATM_BAND)]
    atm_spread_med = float(atm["rel_spread"].median()) if len(atm) else None

    band = df[df["strike"].sub(close).abs() <= close * MONEYNESS_BAND]
    n_two_sided = int(band.loc[band["two_sided"], "strike"].nunique())

    return {
        "atm_spread_med": atm_spread_med,
        "n_atm_quotes": int(len(atm)),
        "n_two_sided_strikes": n_two_sided,
        "n_chain_rows": int(len(df)),
    }


def check_candidate(symbol: str) -> dict:
    """对候选标的实拉一条链做体检（钉最接近 30 DTE 的月度）。"""
    symbol = symbol.upper()
    try:
        asof, close = fetch.latest_trading_day(symbol)
    except Exception as e:
        return {"ok": False, "symbol": symbol, "error": f"标的收盘价获取失败：{str(e)[:120]}"}
    expiries = fetch.monthly_expirations(symbol, asof)
    if not expiries:
        return {"ok": False, "symbol": symbol,
                "error": "无 7–60 DTE 月度到期（可能无期权或链未挂牌）"}
    expiry = min(expiries, key=lambda e: abs((e - asof).days - 30))
    raw = fetch.fetch_chain_eod(symbol, expiry, asof)
    chain = pd.DataFrame({
        "strike": raw["strike"].astype(float),
        "right": raw["right"],
        "bid": raw["bid"].astype(float),
        "ask": raw["ask"].astype(float),
    })
    m = _metrics_from_chain(chain, close)
    checks = {
        "atm_spread_ok": m["atm_spread_med"] is not None
                         and m["atm_spread_med"] <= MAX_ATM_SPREAD,
        "coverage_ok": m["n_two_sided_strikes"] >= MIN_TWO_SIDED,
    }
    return {
        "ok": True, "symbol": symbol, "date": str(asof), "expiry": str(expiry),
        "close": close, "metrics": m, "checks": checks,
        "verdict": all(checks.values()),
        "thresholds": {"max_atm_spread": MAX_ATM_SPREAD, "min_two_sided": MIN_TWO_SIDED},
    }


def pool_reference() -> list[dict]:
    """在池标的的同口径指标（从库里最新链算，免拉取）——给候选一个参照系。"""
    conn = db.get_conn()
    out = []
    for sym in fetch_pool_symbols():
        row = conn.execute(
            "SELECT date, expiry FROM rnd_indicators WHERE symbol=? AND pinned=1"
            " ORDER BY date DESC LIMIT 1", (sym,)).fetchone()
        if row is None:
            continue
        d, e = row
        chain = pd.read_sql_query(
            "SELECT strike, right, bid, ask, underlying_close FROM raw_chain"
            " WHERE date=? AND symbol=? AND expiry=?", conn, params=(d, sym, e))
        if chain.empty:
            continue
        m = _metrics_from_chain(chain[["strike", "right", "bid", "ask"]],
                                float(chain["underlying_close"].iloc[0]))
        out.append({"symbol": sym, "date": d} | m)
    conn.close()
    return out


def fetch_pool_symbols() -> list[str]:
    import yaml
    from .config import PROJECT_ROOT
    cfg = yaml.safe_load((PROJECT_ROOT / "symbols.yaml").read_text())
    return list(cfg.get("fixed", [])) + list(cfg.get("dynamic") or [])

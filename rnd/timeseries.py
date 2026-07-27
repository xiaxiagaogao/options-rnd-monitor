"""指标时序后处理（spec §1）：钉最接近 30 DTE + roll 日打标 + term_slope。

全部从 rnd_indicators 已有行推导，可反复重跑（幂等）。
"""
import pandas as pd

PIN_DTE = 30


def postprocess_symbol(conn, symbol: str):
    df = pd.read_sql_query(
        "SELECT date, expiry, dte, atm_iv FROM rnd_indicators WHERE symbol = ? "
        "ORDER BY date, dte", conn, params=(symbol,))
    if df.empty:
        return
    updates_term, updates_pin = [], []
    for date, g in df.groupby("date"):
        g = g.sort_values("dte")
        # term_slope：近月 ATM IV − 次月 ATM IV（当日恰有两个到期时）
        slope = float(g.iloc[0]["atm_iv"] - g.iloc[1]["atm_iv"]) if len(g) >= 2 else None
        for _, row in g.iterrows():
            updates_term.append((slope, date, symbol, row["expiry"]))
        # 钉最接近 30 DTE
        pinned_expiry = g.loc[(g["dte"] - PIN_DTE).abs().idxmin(), "expiry"]
        updates_pin.append((date, pinned_expiry))

    conn.executemany(
        "UPDATE rnd_indicators SET term_slope = ? WHERE date = ? AND symbol = ? AND expiry = ?",
        updates_term)
    conn.execute("UPDATE rnd_indicators SET pinned = 0, roll = 0 WHERE symbol = ?", (symbol,))
    conn.executemany(
        "UPDATE rnd_indicators SET pinned = 1 WHERE symbol = ? AND date = ? AND expiry = ?",
        [(symbol, date, expiry) for date, expiry in updates_pin])
    # roll 日 = 钉住的到期与上一交易日不同（不打标会在换月日产生锯齿假信号）
    prev = None
    for date, expiry in updates_pin:
        if prev is not None and expiry != prev:
            conn.execute(
                "UPDATE rnd_indicators SET roll = 1 WHERE symbol = ? AND date = ? AND expiry = ?",
                (symbol, date, expiry))
        prev = expiry
    conn.commit()

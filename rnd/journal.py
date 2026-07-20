"""交易日志的 roll_repin（spec §6：跨到期持仓在 roll 日重钉下一月度的对应分位）。

自愈式：不依赖恰好在 roll 日当天运行——只要开仓持仓的 frozen_expiry 与当前
pinned 到期不一致，就用最新 pinned 行的分位快照追加 roll_repin 事件。
append-only：旧冻结线永不修改，重钉是新事件（§6 预定义规则的执行痕迹）。
"""


def _open_positions(conn):
    q = """
      SELECT t.* FROM trade_journal t
      JOIN (SELECT position_id, MAX(event_id) AS mid FROM trade_journal
            WHERE event_type IN ('open','roll_repin') GROUP BY position_id) last
        ON last.mid = t.event_id
      WHERE t.position_id NOT IN
        (SELECT position_id FROM trade_journal WHERE event_type='close')
    """
    cur = conn.execute(q)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def roll_repin_check(conn) -> list[dict]:
    """检查全部开仓持仓，需要时追加 roll_repin。返回重钉记录清单。"""
    repinned = []
    for p in _open_positions(conn):
        row = conn.execute(
            "SELECT date, expiry, q05, q25, q50, q75, q95, sigma1_abs, forward"
            " FROM rnd_indicators WHERE symbol=? AND pinned=1 ORDER BY date DESC LIMIT 1",
            (p["symbol"],)).fetchone()
        if row is None:
            continue
        date, expiry, q05, q25, q50, q75, q95, sigma1, fwd = row
        if expiry == p["frozen_expiry"]:
            continue
        conn.execute(
            """INSERT INTO trade_journal
               (position_id, event_type, event_date, symbol, direction, identity,
                entry_price, frozen_expiry, frozen_q05, frozen_q25, frozen_q50,
                frozen_q75, frozen_q95, frozen_sigma1, frozen_forward, stop_q,
                risk_budget, size, target_price, target_rationale, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p["position_id"], "roll_repin", date, p["symbol"], p["direction"],
             p["identity"], p["entry_price"], expiry, q05, q25, q50, q75, q95,
             sigma1, fwd, p["stop_q"], p["risk_budget"], p["size"],
             p["target_price"], p["target_rationale"],
             f"roll 重钉：{p['frozen_expiry']} → {expiry}（§6 预定义规则）"))
        repinned.append({"position_id": p["position_id"], "symbol": p["symbol"],
                         "from": p["frozen_expiry"], "to": expiry, "date": date})
    conn.commit()
    return repinned

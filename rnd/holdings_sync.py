"""持仓同步（holdings-sync-framework）：读 fund.db 币安持仓 → 映射过滤 → 驱动标的池。

只读同机 fund.db（真金白银账本，绝不写）；本机无库 → no-op 降级。
"""
import datetime as dt
import os
import sqlite3
from pathlib import Path

FUND_DB_PATH = os.getenv("FUND_DB_PATH", "data/fund.db")  # 币安基金看板 fund.db 路径，配于 .env（见 .env.example）；不存在 → 持仓同步降级 no-op
HELD_EPS = 1e-9

# 无美股期权 → 排除（framework §6.2）。值为原因，仅供告警可读。
SYMBOL_BLACKLIST = {
    "SAMSUNGUSDT": "韩股，无美股期权",
    "SKHYNIXUSDT": "韩股，无美股期权",
    "OPENAIUSDT": "未上市（合成）",
    "SPCXUSDT": "未上市（SpaceX 合成）",
    "DRAMUSDT": "合成主题，无对应美股",
    "XAUUSDT": "商品（黄金）",
    "XAGUSDT": "商品（白银）",
    "CLUSDT": "商品（WTI 原油）",
    "BZUSDT": "商品（布伦特原油，非美股 BZ）",
}


def map_symbol(binance_symbol: str) -> str | None:
    """币安 symbol → 美股 ticker。黑名单或非 USDT 结尾 → None。"""
    s = binance_symbol.upper()
    if s in SYMBOL_BLACKLIST:
        return None
    if not s.endswith("USDT") or len(s) <= 4:
        return None
    return s[:-4]


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def _blank_cycle() -> dict:
    return {"net": 0.0, "entry_qty": 0.0, "entry_quote": 0.0,
            "entry_time": None, "num_opening": 0}


def _walk_cycles(rows) -> dict[tuple[str, str], dict]:
    """持仓周期走查。**口径对齐 fund-dashboard `backend/positions/derive.go`**——
    两个面板必须给出同一个开仓均价，否则用户会看到两套数字打架。

    rows 每项 (symbol, position_side, side, qty, quote_qty|None, fill_time|None)，
    须按 fill_time 升序。返回 {(symbol, position_side): 周期状态}，只含走查结束时
    仍未平（net != 0）的周期 —— 即**当前持仓**。

    规则（逐条对应 derive.go）：
      · 开仓/加仓：进 entry 腿，按 quote 金额累加 → 均价 = entry_quote / entry_qty
        （所以低位加仓会摊低均价，与币安 App 的 Entry Price 一致）
      · 部分平仓：只减净仓，**不改均价**（平仓腿在 derive.go 里单独累计，此处不需要）
      · 净仓归零：周期结束，状态清空（再开则是全新周期）
      · 单笔跨零翻向：按数量比例切分，剩余部分开新周期
    """
    st: dict[tuple[str, str], dict] = {}
    for symbol, pos_side, side, qty, quote, ft in rows:
        key = (symbol, pos_side)
        signed = qty if side == "BUY" else -qty
        cur = st.get(key) or _blank_cycle()
        if cur["net"] == 0 or _sign(cur["net"]) == _sign(signed):
            if cur["entry_qty"] == 0:          # 周期起点
                cur["entry_time"] = ft
            cur["entry_qty"] += qty
            cur["entry_quote"] += quote or 0.0
            cur["num_opening"] += 1
            cur["net"] += signed
        elif abs(signed) <= abs(cur["net"]) + HELD_EPS:
            cur["net"] += signed               # 平仓：均价不动
            if abs(cur["net"]) < HELD_EPS:
                cur = _blank_cycle()
        else:                                   # 跨零翻向：切分
            close_qty = abs(cur["net"])
            frac = close_qty / qty if qty else 0.0
            cur = {"net": cur["net"] + signed,
                   "entry_qty": qty - close_qty,
                   "entry_quote": (quote or 0.0) * (1 - frac),
                   "entry_time": ft, "num_opening": 1}
        st[key] = cur
    return {k: v for k, v in st.items() if abs(v["net"]) > HELD_EPS}


def derive_current_holdings(fund_db_path: str | Path = FUND_DB_PATH) -> set[str]:
    """读 fund.db binance_fills，按 (symbol,position_side) derive 净持仓。
    对齐基金 positions/derive.go：BUY +qty / SELL -qty，abs > 1e-9 判持有。
    库不存在（本机开发）→ 空集合。绝不写库。"""
    p = Path(fund_db_path)
    if not p.exists():
        return set()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = [(s, ps, sd, q, None, None) for s, ps, sd, q in conn.execute(
            "SELECT symbol, position_side, side, qty FROM binance_fills"
            " ORDER BY rowid").fetchall()]
    finally:
        conn.close()
    return {sym for (sym, _) in _walk_cycles(rows)}


def entry_dates(conn, fund_db_path: str | Path = FUND_DB_PATH) -> dict:
    """每个当前持仓标的的开仓点（binance-entry-anchor spec §3.1）。只读 fund.db。

    conn：rnd 库连接（用于把开仓日 snap 到最近有 RND 曲线的交易日）。
    返回 {ticker: {open_date, rnd_date, entry_price, qty, num_opening_fills}}：
      open_date   当前持仓周期的起始日（净仓由 0 转非 0 那笔，UTC 日历日 ISO）
      rnd_date    ≤ open_date 的最近有 rnd_indicators 的交易日；无更早曲线 → None
      entry_price **quote 加权平均开仓价**（口径同 fund 看板与币安 App 的 Entry
                  Price：低位加仓会摊低，部分平仓不改）。代币化永续价，仅展示，
                  不入 RND 计算。
      qty         当前净仓（带方向符号）
      num_opening_fills 本周期开仓笔数（>1 表示均价是多笔加权出来的）
    fund.db 不存在 → {}。同一 ticker 多方向并存取净敞口更大者（v0 简化，见 spec §6）。"""
    p = Path(fund_db_path)
    if not p.exists():
        return {}
    fconn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = fconn.execute(
            "SELECT symbol, position_side, side, qty, quote_qty, fill_time "
            "FROM binance_fills ORDER BY fill_time").fetchall()
    finally:
        fconn.close()
    out: dict[str, dict] = {}
    for (symbol, _pos), st in _walk_cycles(rows).items():
        ticker = map_symbol(symbol)
        if ticker is None or st["entry_time"] is None or st["entry_qty"] <= 0:
            continue
        open_date = dt.datetime.fromtimestamp(
            st["entry_time"] / 1000, dt.timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT MAX(date) FROM rnd_indicators WHERE symbol=? AND date<=?",
            (ticker, open_date)).fetchone()
        prev = out.get(ticker)
        if prev is None or abs(st["net"]) > abs(prev["qty"]):   # 多方向取净敞口更大者
            out[ticker] = {
                "open_date": open_date,
                "rnd_date": row[0] if row and row[0] else None,
                "entry_price": st["entry_quote"] / st["entry_qty"],
                "qty": st["net"],
                "num_opening_fills": st["num_opening"],
            }
    return out


def resolve_holdings(fund_db_path: str | Path = FUND_DB_PATH) -> tuple[set[str], set[str]]:
    """derive + map。返回 (可映射美股 ticker 集合, 被排除的原始 binance symbol 集合)。"""
    mapped, excluded = set(), set()
    for bsym in derive_current_holdings(fund_db_path):
        t = map_symbol(bsym)
        if t is None:
            excluded.add(bsym)
        else:
            mapped.add(t)
    return mapped, excluded


def sync(conn, fund_db_path: str | Path = FUND_DB_PATH,
         gate_fn=None, yaml_path=None) -> dict:
    """每日同步：fund.db 当前持仓 → 映射过滤 → 新标的过闸门 → 写 symbols.yaml。

    纯计算 + 写 yaml；backfill/TG 副作用由调用方按返回 diff 触发。
    conn：rnd db（读 open journal for pin）。
    gate_fn(ticker)->bool：默认 admission.check_candidate(...)['verdict']（会实拉链）；
        测试注入假闸门。仅对新候选调用（已在池的不重复体检）。gate_fn 逐标的异常隔离：
        单个标的检查出错不影响其他候选，本次跳过、下次再试。
    返回 {added, removed, pinned, rejected, excluded, gate_errors} 或 {skipped}。
    """
    from server import pool
    from rnd import journal
    p = Path(fund_db_path)
    if not p.exists():
        return {"skipped": f"fund.db 不存在（{fund_db_path}），跳过持仓同步"}
    if gate_fn is None:
        from rnd import admission
        gate_fn = lambda s: bool(admission.check_candidate(s).get("verdict"))

    mapped, excluded = resolve_holdings(fund_db_path)
    cur = pool.read_pool(yaml_path)
    baseline = set(cur["baseline"])
    prev_holdings = set(cur["holdings"])
    pinned = journal.open_symbols(conn)

    candidates = mapped - baseline          # 当前持仓映射后、去基准
    # 曾在池（holdings 或盘上旧 pinned）= 已建立，不重复体检。关键：用盘上旧 pinned
    # cur["pinned"] 而非新算的 pinned——否则标的从 pinned 降级（journal 平仓但仍持有）
    # 会被误当全新标的：重复过闸门 + spurious backfill + 误报"新纳入"，gate 当天若失败还会掉出池。
    established = prev_holdings | set(cur["pinned"])
    passed, rejected, gate_errors = set(), set(), set()
    for s in sorted(candidates - established - pinned):  # 只对真正的新标的过闸门
        try:
            ok = gate_fn(s)
        except Exception as e:  # noqa: BLE001
            gate_errors.add(s)
            print(f"  持仓同步: {s} 闸门检查出错，本次跳过（{type(e).__name__}: {e}）")
            continue
        (passed if ok else rejected).add(s)

    new_holdings = (candidates & established) | passed   # 仍持有的已建立标的（含从 pinned 降级）+ 新过闸门
    new_holdings -= pinned                  # pinned 独立成组，不重复进 holdings
    added = new_holdings - prev_holdings - set(cur["pinned"])   # 真新增（排除从 pinned 降级，避免重复 backfill/误报）
    removed = (prev_holdings - new_holdings) - pinned

    if new_holdings != prev_holdings or set(pinned) != set(cur["pinned"]):
        pool.write_pool(cur["baseline"], sorted(new_holdings), sorted(pinned), yaml_path)
    return {
        "added": sorted(added), "removed": sorted(removed),
        "pinned": sorted(pinned), "rejected": sorted(rejected),
        "excluded": sorted(excluded), "gate_errors": sorted(gate_errors),
    }

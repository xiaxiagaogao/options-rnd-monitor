"""持仓同步（holdings-sync-framework）：读 fund.db 币安持仓 → 映射过滤 → 驱动标的池。

只读同机 fund.db（真金白银账本，绝不写）；本机无库 → no-op 降级。
"""
import os
import sqlite3
from pathlib import Path

FUND_DB_PATH = os.getenv("FUND_DB_PATH", "<FUND_DB_PATH>")
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


def derive_current_holdings(fund_db_path: str | Path = FUND_DB_PATH) -> set[str]:
    """读 fund.db binance_fills，按 (symbol,position_side) derive 净持仓。
    对齐基金 positions/derive.go：BUY +qty / SELL -qty，abs > 1e-9 判持有。
    库不存在（本机开发）→ 空集合。绝不写库。"""
    p = Path(fund_db_path)
    if not p.exists():
        return set()
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT symbol, position_side, side, qty FROM binance_fills").fetchall()
    finally:
        conn.close()
    net: dict[tuple[str, str], float] = {}
    for symbol, pos_side, side, qty in rows:
        signed = qty if side == "BUY" else -qty
        key = (symbol, pos_side)
        net[key] = net.get(key, 0.0) + signed
    return {sym for (sym, _), n in net.items() if abs(n) > HELD_EPS}


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
    already = prev_holdings | pinned
    passed, rejected, gate_errors = set(), set(), set()
    for s in sorted(candidates - already):  # 只对新候选过闸门
        try:
            ok = gate_fn(s)
        except Exception as e:  # noqa: BLE001
            gate_errors.add(s)
            print(f"  持仓同步: {s} 闸门检查出错，本次跳过（{type(e).__name__}: {e}）")
            continue
        (passed if ok else rejected).add(s)

    new_holdings = (candidates & prev_holdings) | passed
    new_holdings -= pinned                  # pinned 独立成组，不重复进 holdings
    added = new_holdings - prev_holdings
    removed = (prev_holdings - new_holdings) - pinned

    if new_holdings != prev_holdings or set(pinned) != set(cur["pinned"]):
        pool.write_pool(cur["baseline"], sorted(new_holdings), sorted(pinned), yaml_path)
    return {
        "added": sorted(added), "removed": sorted(removed),
        "pinned": sorted(pinned), "rejected": sorted(rejected),
        "excluded": sorted(excluded), "gate_errors": sorted(gate_errors),
    }

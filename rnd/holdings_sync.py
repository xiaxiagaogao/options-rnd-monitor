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
    net: dict[tuple, float] = {}
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
        (excluded.add(bsym) if t is None else mapped.add(t))
    return mapped, excluded

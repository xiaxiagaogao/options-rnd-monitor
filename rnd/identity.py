"""币安持仓身份登记（holding-identity spec，2026-10-19）。

身份跟标的走、只追加：holding_identity 表每改一次记一行，当前身份 = 该标的 event_id 最大的一行。
选线规则只在这里定义（spec §4，主 spec §6 纪律）：投机钉 Q25、信念钉 Q05；空头镜像为 Q75 / Q95。
"""
from datetime import datetime

IDENTITIES = ("speculative", "conviction")
LABEL = {"speculative": "投机", "conviction": "信念"}
_STOP = {("speculative", True): "q25", ("conviction", True): "q05",
         ("speculative", False): "q75", ("conviction", False): "q95"}
_COLS = "symbol, identity, prev_identity, changed_at"


def stop_quantile(identity: str | None, long: bool) -> str | None:
    """身份 → 失效分位；未登记返回 None（调用方两线并挂）。"""
    return _STOP.get((identity, bool(long)))


def line_pair(long: bool) -> dict[str, str]:
    """两条候选失效线 {身份: 分位}，未登记时并挂。"""
    return {k: _STOP[(k, bool(long))] for k in IDENTITIES}


def _rec(row) -> dict:
    return {"symbol": row[0], "identity": row[1], "prev_identity": row[2], "changed_at": row[3]}


def current(conn) -> dict[str, dict]:
    """{symbol: 最新一条记录}；从未登记的标的不在字典里。"""
    rows = conn.execute(
        f"SELECT {_COLS} FROM holding_identity h"
        " WHERE event_id = (SELECT MAX(event_id) FROM holding_identity WHERE symbol = h.symbol)")
    return {r[0]: _rec(r) for r in rows}


def history(conn, symbol: str) -> list[dict]:
    """该标的全部记录，新的在前。"""
    rows = conn.execute(f"SELECT {_COLS} FROM holding_identity WHERE symbol = ?"
                        " ORDER BY event_id DESC", (symbol.upper(),))
    return [_rec(r) for r in rows]


def set_identity(conn, symbol: str, identity: str, *, now: datetime | None = None) -> dict:
    """登记 / 改动身份。与当前相同 → 不写行（changed=False）。非法身份 → ValueError。"""
    if identity not in IDENTITIES:
        raise ValueError(f"身份只能是 {' / '.join(IDENTITIES)}，收到 {identity!r}")
    symbol = symbol.upper()
    hist = history(conn, symbol)
    cur = hist[0] if hist else None
    if cur and cur["identity"] == identity:
        return cur | {"changed": False}
    prev = cur["identity"] if cur else None
    ts = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    conn.execute("INSERT INTO holding_identity (symbol, identity, prev_identity, changed_at)"
                 " VALUES (?, ?, ?, ?)", (symbol, identity, prev, ts))
    conn.commit()
    return {"symbol": symbol, "identity": identity, "prev_identity": prev,
            "changed_at": ts, "changed": True}


def change_note(rec: dict | None) -> str | None:
    """白话：「10-19 登记为信念」/「10-25 由投机改为信念」。"""
    if not rec:
        return None
    day = rec["changed_at"][5:10]
    if rec.get("prev_identity"):
        return f"{day} 由{LABEL[rec['prev_identity']]}改为{LABEL[rec['identity']]}"
    return f"{day} 登记为{LABEL[rec['identity']]}"

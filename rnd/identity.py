"""标的身份登记（holding-identity spec，2026-10-19）。

身份跟标的走、只追加：holding_identity 表每改一次记一行，当前身份 = 该标的 event_id 最大的一行。
2026-10 起登记不再要求当前持有，只要求标的在 RND 池内。
选线规则只在这里定义（spec §4，主 spec §6 纪律）：投机钉 Q25、信念钉 Q05；空头镜像为 Q75 / Q95。
"""
import datetime as dt
from zoneinfo import ZoneInfo

IDENTITIES = ("speculative", "conviction")
LABEL = {"speculative": "投机", "conviction": "信念"}
_STOP = {("speculative", True): "q25", ("conviction", True): "q05",
         ("speculative", False): "q75", ("conviction", False): "q95"}
_ET = ZoneInfo("America/New_York")
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


def as_of(conn, symbol: str, asof: str) -> dict | None:
    """该标的在 asof 当天结束时的身份（回放用）：取 changed_at 折成美东日期后 ≤ asof 的最新一行。

    asof 是美东交易日（和 RND 日期同一口径），changed_at 带写入时的时区偏移（见 DDL），
    所以先换算到 America/New_York 再取日期；无偏移的旧数据按 UTC 解释。同一时刻多行按 event_id 大者优先。
    返回 {identity, changed_at}；无记录/日期非法 → None。
    """
    try:
        end = dt.date.fromisoformat(asof)
    except (TypeError, ValueError):
        return None
    rows = conn.execute(
        f"SELECT event_id, {_COLS} FROM holding_identity WHERE symbol = ?"
        " ORDER BY event_id DESC", (symbol.upper(),)).fetchall()
    best = None
    best_key = None
    for eid, _sym, idn, _prev, ts in rows:
        try:
            parsed = dt.datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        # aware 时间按真实时刻比；无偏移的旧数据按 UTC 解释，避免依赖本机时区。
        aware = parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        if aware.astimezone(_ET).date() > end:
            continue
        instant = aware.timestamp()
        key = (instant, eid)
        if best_key is None or key > best_key:
            best_key = key
            best = {"identity": idn, "changed_at": ts}
    return best


def set_identity(conn, symbol: str, identity: str, *, now: dt.datetime | None = None) -> dict:
    """登记 / 改动身份。与当前相同 → 不写行（changed=False）。非法身份 → ValueError。"""
    if identity not in IDENTITIES:
        raise ValueError(f"身份只能是 {' / '.join(IDENTITIES)}，收到 {identity!r}")
    symbol = symbol.upper()
    hist = history(conn, symbol)
    cur = hist[0] if hist else None
    if cur and cur["identity"] == identity:
        return cur | {"changed": False}
    prev = cur["identity"] if cur else None
    ts = (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds")
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

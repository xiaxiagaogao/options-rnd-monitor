"""自选清单（watchlist spec，2026-11-07）：RND 池的唯一来源。

- watchlist 表存当前状态（可改），每次改动在 watchlist_events 追加一条流水。
- 角色不存这里：当前角色 = holding_identity 该标的最新一行（rnd/identity.py），改角色只追加。
- 池 = stage 为 ready 的条目（server/pool.effective_symbols 读这里）。
- 准入检查与回填由 scripts/watchlist_worker.py 在 ThetaData 锁内串行推进 stage。
"""
import datetime as dt
import hashlib
import json
import re

from . import identity as ident

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")   # 与 server/api.py 的 SYMBOL_RE 同口径
BINANCE_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
STAGES = ("queued", "checking", "rejected", "backfilling", "ready", "error")
_PENDING_REASON = {"queued": "等待准入检查", "checking": "准入检查中", "backfilling": "回填历史数据中"}


class WatchlistError(ValueError):
    """清单操作的业务错误（重复加入、非法输入、标的不在清单等），路由层转 400。"""


def _now(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now().astimezone()).isoformat(timespec="seconds")


def default_binance(ticker: str) -> str:
    """默认币安合约名：ticker 去掉 "." 和 "-" 后加 USDT（BRK.B → BRKBUSDT）。"""
    return re.sub(r"[.\-]", "", ticker.upper()) + "USDT"


def _ticker(ticker: str) -> str:
    t = str(ticker or "").strip().upper()
    if not TICKER_RE.match(t):
        raise WatchlistError(f"非法代码：{ticker!r}")
    return t


def _check_role(ticker: str, role: str):
    if role not in ident.IDENTITIES:
        raise WatchlistError("角色只能是 index（指数）/ conviction（信念）/ speculative（投机）")
    if role == "index" and ticker not in ident.INDEX_TICKERS:
        raise WatchlistError(f"指数角色只许 {' / '.join(ident.INDEX_TICKERS)}")


def _binance(binance: str | None, ticker: str) -> str:
    b = str(binance).strip().upper() if binance else default_binance(ticker)
    if not BINANCE_RE.match(b):
        raise WatchlistError(f"币安合约名须形如 NVDAUSDT：{binance!r}")
    return b


def _event(conn, ticker: str, action: str, detail: dict | None, at: str):
    conn.execute("INSERT INTO watchlist_events (ticker, action, detail, at) VALUES (?, ?, ?, ?)",
                 (ticker, action, json.dumps(detail, ensure_ascii=False) if detail else None, at))


def _row(conn, ticker: str):
    return conn.execute("SELECT ticker, binance, stage, reason, check_json, added_at, updated_at"
                        " FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()


def _require(conn, ticker: str):
    row = _row(conn, ticker)
    if row is None:
        raise WatchlistError(f"{ticker} 不在自选清单")
    return row


def status_of(stage: str) -> str:
    return {"ready": "active", "rejected": "rejected"}.get(stage, "pending")


def _entry(row, roles: dict) -> dict:
    ticker, binance, stage, reason, check_json, added_at, _updated = row
    role = roles.get(ticker)
    status = status_of(stage)
    if status == "active":
        reason = None
    elif status == "pending" and stage != "error":
        reason = _PENDING_REASON[stage]
    return {
        "ticker": ticker, "role": role["identity"] if role else None, "binance": binance,
        "status": status, "reason": reason, "added_at": added_at,
        "role_changed_at": role["changed_at"] if role else None,
        # 以下只给面板用，payload() 会剥掉
        "stage": stage, "check": json.loads(check_json) if check_json else None,
    }


def entries(conn) -> list[dict]:
    """全部条目（含面板字段 stage / check），按 ticker 升序。"""
    roles = ident.current(conn)
    rows = conn.execute("SELECT ticker, binance, stage, reason, check_json, added_at, updated_at"
                        " FROM watchlist ORDER BY ticker").fetchall()
    return [_entry(r, roles) for r in rows]


def entry(conn, ticker: str) -> dict:
    return _entry(_require(conn, ticker), ident.current(conn))


def is_seeded(conn) -> bool:
    """清单是否被写过（有任何流水）。从没写过 = 还没迁移，池退回 symbols.yaml。"""
    return conn.execute("SELECT 1 FROM watchlist_events LIMIT 1").fetchone() is not None


def ready_tickers(conn) -> list[str]:
    """池：stage=ready。SPY / QQQ 排前（沿用旧池的 baseline 在前），其余按字母。"""
    got = {r[0] for r in conn.execute("SELECT ticker FROM watchlist WHERE stage = 'ready'")}
    head = [t for t in ident.INDEX_TICKERS if t in got]
    return head + sorted(got - set(head))


def add(conn, ticker: str, role: str, binance: str | None = None, *,
        stage: str = "queued", now: dt.datetime | None = None, source: str | None = None,
        commit: bool = True) -> dict:
    """加入清单。先全部校验再写；清单行、身份行、流水在同一事务里提交（commit=False 由调用方提交）。"""
    t = _ticker(ticker)
    _check_role(t, role)
    b = _binance(binance, t)
    if stage not in STAGES:
        raise WatchlistError(f"非法 stage：{stage}")
    if _row(conn, t) is not None:
        raise WatchlistError(f"{t} 已在自选清单")
    at = _now(now)
    try:
        conn.execute("INSERT INTO watchlist (ticker, binance, stage, added_at, updated_at)"
                     " VALUES (?, ?, ?, ?, ?)", (t, b, stage, at, at))
        ident.set_identity(conn, t, role, now=now, commit=False)   # 相同身份不新增行
        _event(conn, t, "add", {"role": role, "binance": b, "stage": stage,
                                **({"source": source} if source else {})}, at)
    except Exception:
        conn.rollback()
        raise
    if commit:
        conn.commit()
    return entry(conn, t)


def update(conn, ticker: str, *, role: str | None = None, binance: str | None = None,
           now: dt.datetime | None = None) -> dict:
    """改角色 / 合约名。先全部校验再写，同一事务提交——不会出现「角色改了、合约名报错」的半写。"""
    t = _ticker(ticker)
    row = _require(conn, t)
    if role is not None:
        _check_role(t, role)
    b = _binance(binance, t) if binance is not None else None
    at = _now(now)
    try:
        if role is not None:
            rec = ident.set_identity(conn, t, role, now=now, commit=False)
            if rec["changed"]:
                _event(conn, t, "role", {"from": rec["prev_identity"], "to": role}, at)
        if b is not None and b != row[1]:
            conn.execute("UPDATE watchlist SET binance = ?, updated_at = ? WHERE ticker = ?", (b, at, t))
            _event(conn, t, "binance", {"from": row[1], "to": b}, at)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return entry(conn, t)


def record_role_change(conn, ticker: str, rec: dict):
    """标的页持仓卡直接改身份（不经清单）时补一条 role 流水，让 /api/watchlist 的 updated_at 跟上。"""
    if rec.get("changed") and _row(conn, ticker) is not None:
        _event(conn, ticker, "role", {"from": rec.get("prev_identity"), "to": rec["identity"],
                                      "source": "symbol_card"}, rec["changed_at"])
        conn.commit()


def remove(conn, ticker: str, *, now: dt.datetime | None = None):
    """移出清单 = 移出池、停止每日拉取；rnd_* 历史与身份记录都保留。"""
    t = _ticker(ticker)
    row = _require(conn, t)
    conn.execute("DELETE FROM watchlist WHERE ticker = ?", (t,))
    _event(conn, t, "remove", {"stage": row[2]}, _now(now))
    conn.commit()


def recheck(conn, ticker: str, *, now: dt.datetime | None = None) -> dict:
    t = _ticker(ticker)
    row = _require(conn, t)
    if row[2] not in ("rejected", "error"):
        raise WatchlistError(f"{t} 当前不需要重新检查")
    set_stage(conn, t, "queued", now=now)
    return entry(conn, t)


def set_stage(conn, ticker: str, stage: str, *, reason: str | None = None,
              check: dict | None = None, now: dt.datetime | None = None) -> bool:
    """推进 stage（worker 用）。行已被删（处理中途移除）→ 返回 False，不复活。"""
    if stage not in STAGES:
        raise WatchlistError(f"非法 stage：{stage}")
    at = _now(now)
    sets, args = ["stage = ?", "reason = ?", "updated_at = ?"], [stage, reason, at]
    if check is not None:
        sets.append("check_json = ?")
        args.append(json.dumps(check, ensure_ascii=False, default=str))
    cur = conn.execute(f"UPDATE watchlist SET {', '.join(sets)} WHERE ticker = ?", (*args, ticker))
    if cur.rowcount == 0:
        conn.commit()
        return False
    _event(conn, ticker, "stage", {"to": stage, **({"reason": reason} if reason else {})}, at)
    conn.commit()
    return True


def reset_stale(conn) -> list[str]:
    """worker 启动时：上一个 worker 被杀 / 机器重启留下的 checking / backfilling 改回 queued。"""
    stale = [r[0] for r in conn.execute(
        "SELECT ticker FROM watchlist WHERE stage IN ('checking', 'backfilling')")]
    for t in stale:
        set_stage(conn, t, "queued", reason=None)
    return stale


def needs_worker(conn) -> list[str]:
    """需要 worker 的条目：待处理（queued / error）+ 残留的 checking / backfilling（worker 被杀、
    机器重启留下的）。eod_update 收尾据此决定要不要起 worker；worker 启动时先把残留复位。"""
    return [r[0] for r in conn.execute(
        "SELECT ticker FROM watchlist WHERE stage IN ('queued', 'error', 'checking', 'backfilling')"
        " ORDER BY added_at, ticker")]


def pending_jobs(conn) -> list[str]:
    """worker 待处理：queued + error（error 每次 worker 运行重试一次），按加入顺序。"""
    return [r[0] for r in conn.execute(
        "SELECT ticker FROM watchlist WHERE stage IN ('queued', 'error') ORDER BY added_at, ticker")]


def canonical_json(value) -> str:
    """与 server/context.canonical_json 同一套规则：键排序、无空格、不转义中文。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def payload(conn) -> dict:
    """GET /api/watchlist 的响应体（watchlist spec §6.1）。"""
    public = [{k: e[k] for k in ("ticker", "role", "binance", "status", "reason",
                                 "added_at", "role_changed_at")} for e in entries(conn)]
    # updated_at = 清单流水与在单标的角色变化里最晚的那个（标的页也能改身份）；按真实时刻比，不按字符串
    stamps = [r[0] for r in conn.execute("SELECT at FROM watchlist_events ORDER BY event_id DESC LIMIT 1")]
    stamps += [e["role_changed_at"] for e in public if e["role_changed_at"]]
    return {
        "version": hashlib.sha256(canonical_json(public).encode("utf-8")).hexdigest(),
        "updated_at": max(stamps, key=_instant) if stamps else None,
        "entries": public,
    }


def _instant(ts: str) -> float:
    d = dt.datetime.fromisoformat(ts)
    return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()

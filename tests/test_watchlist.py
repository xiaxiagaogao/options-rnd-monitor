"""自选清单数据层（watchlist spec §4 / §6.1）。临时库，不触网。

    .venv/bin/python tests/test_watchlist.py
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import db, identity as ident, watchlist as wl

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


SGT = timezone(timedelta(hours=8))
T = [datetime(2026, 11, 8, 9, i, tzinfo=SGT) for i in range(30)]


def raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except wl.WatchlistError as e:
        return str(e)
    return None


def add_pinned_row(conn, sym, date="2026-11-06"):
    db.upsert_indicators(conn, {"date": date, "symbol": sym, "expiry": "2026-11-20",
                                "dte": 14, "gate_pass": 1, "pinned": 1, "roll": 0})
    conn.commit()


print("[默认合约名 / 校验]")
check("NVDA → NVDAUSDT", wl.default_binance("NVDA") == "NVDAUSDT")
check("BRK.B → BRKBUSDT（去掉点）", wl.default_binance("brk.b") == "BRKBUSDT")
check("RDS-A → RDSAUSDT（去掉横杠）", wl.default_binance("RDS-A") == "RDSAUSDT")

with tempfile.TemporaryDirectory() as tmp:
    conn = db.get_conn(Path(tmp) / "w.sqlite")
    check("空库：从没写过清单", wl.is_seeded(conn) is False and wl.entries(conn) == [])

    print("[加入]")
    e = wl.add(conn, "nvda", "conviction", now=T[0])
    check("加入后 queued → status pending、reason 说明在等检查",
          e["ticker"] == "NVDA" and e["status"] == "pending" and e["stage"] == "queued"
          and "准入" in (e["reason"] or ""), str(e))
    check("默认合约名", e["binance"] == "NVDAUSDT", e["binance"])
    check("角色写进 holding_identity", ident.current(conn)["NVDA"]["identity"] == "conviction")
    check("加入即算「写过清单」", wl.is_seeded(conn) is True)
    check("重复加入被拒", raises(wl.add, conn, "NVDA", "speculative") is not None)
    check("非法代码被拒", raises(wl.add, conn, "1BAD", "speculative") is not None)
    check("非法角色被拒", raises(wl.add, conn, "AMD", "yolo") is not None)
    check("指数只许 SPY/QQQ", raises(wl.add, conn, "AMD", "index") is not None)
    check("非法合约名被拒", raises(wl.add, conn, "AMD", "speculative", "amd-perp") is not None)
    n = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]
    check("被拒的加入都没写行", n == 1, f"rows={n}")
    e = wl.add(conn, "SPY", "index", "SPYUSDT", now=T[1])
    check("SPY 可设指数", e["role"] == "index")

    print("[池 = ready]")
    check("queued 不进池", wl.ready_tickers(conn) == [])
    wl.set_stage(conn, "NVDA", "ready", now=T[2])
    wl.set_stage(conn, "SPY", "ready", now=T[2])
    wl.add(conn, "AMD", "speculative", now=T[3])
    wl.set_stage(conn, "AMD", "ready", now=T[3])
    check("ready 进池，SPY/QQQ 排前、其余按字母", wl.ready_tickers(conn) == ["SPY", "AMD", "NVDA"],
          str(wl.ready_tickers(conn)))

    print("[状态映射]")
    wl.add(conn, "FLNC", "speculative", now=T[4])
    wl.set_stage(conn, "FLNC", "rejected", reason="ATM 点差 16.2%（上限 10%）", now=T[5])
    wl.add(conn, "RKLB", "speculative", now=T[6])
    wl.set_stage(conn, "RKLB", "error", reason="回填后仍无可用数据，下次自动重试", now=T[7])
    st = {x["ticker"]: (x["status"], x["reason"]) for x in wl.entries(conn)}
    check("rejected → rejected + 原因", st["FLNC"] == ("rejected", "ATM 点差 16.2%（上限 10%）"), str(st["FLNC"]))
    check("error → pending + 原因", st["RKLB"][0] == "pending" and "回填" in st["RKLB"][1], str(st["RKLB"]))
    check("active 的 reason 为 None", st["NVDA"] == ("active", None), str(st["NVDA"]))
    check("set_stage 对不存在的行返回 False", wl.set_stage(conn, "ZZZ", "ready") is False)

    print("[重新检查 / 复位]")
    check("rejected 可重新检查", wl.recheck(conn, "FLNC", now=T[8])["stage"] == "queued")
    check("ready 不需要重新检查", raises(wl.recheck, conn, "NVDA") is not None)
    wl.set_stage(conn, "FLNC", "checking", now=T[9])
    wl.set_stage(conn, "AMD", "backfilling", now=T[9])
    check("残留 checking/backfilling 复位为 queued", sorted(wl.reset_stale(conn)) == ["AMD", "FLNC"])
    check("待处理 = queued + error（按加入顺序）", wl.pending_jobs(conn) == ["AMD", "FLNC", "RKLB"],
          str(wl.pending_jobs(conn)))
    wl.set_stage(conn, "AMD", "ready", now=T[10])

    print("[改角色 / 合约名 / 移除]")
    e = wl.update(conn, "NVDA", role="speculative", now=T[11])
    check("改角色只追加身份历史", e["role"] == "speculative"
          and [h["identity"] for h in ident.history(conn, "NVDA")] == ["speculative", "conviction"])
    check("role_changed_at = 最新身份行的时间", e["role_changed_at"] == T[11].isoformat(timespec="seconds"))
    e = wl.update(conn, "NVDA", binance="NVDAXUSDT", now=T[12])
    check("改合约名", e["binance"] == "NVDAXUSDT")
    check("改不存在的标的被拒", raises(wl.update, conn, "ZZZ", role="speculative") is not None)
    wl.remove(conn, "AMD", now=T[13])
    check("移除后不在清单、不在池", "AMD" not in [x["ticker"] for x in wl.entries(conn)]
          and "AMD" not in wl.ready_tickers(conn))
    check("移除后身份记录保留", ident.current(conn)["AMD"]["identity"] == "speculative")
    check("移除不存在的被拒", raises(wl.remove, conn, "AMD") is not None)
    e = wl.add(conn, "AMD", "speculative", now=T[14])
    check("重新加入：身份相同不新增行", len(ident.history(conn, "AMD")) == 1 and e["stage"] == "queued")

    print("[对外 payload]")
    p1 = wl.payload(conn)
    check("entries 按 ticker 升序", [x["ticker"] for x in p1["entries"]] == sorted(x["ticker"] for x in p1["entries"]))
    check("字段齐全", set(p1["entries"][0]) == {"ticker", "role", "binance", "status", "reason",
                                               "added_at", "role_changed_at"}, str(sorted(p1["entries"][0])))
    check("updated_at = 最新一条事件时间", p1["updated_at"] == T[14].isoformat(timespec="seconds"), p1["updated_at"])
    check("version 是 64 位十六进制", len(p1["version"]) == 64)
    check("内容不变 → version 不变", wl.payload(conn)["version"] == p1["version"])
    wl.set_stage(conn, "AMD", "ready", now=T[15])
    check("状态变了 → version 变", wl.payload(conn)["version"] != p1["version"])
    conn.close()

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过。")

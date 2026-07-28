"""币安开仓点派生（binance-entry-anchor spec §5）。合成 fills，无网络。

    .venv/bin/python tests/test_holdings_entry.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import holdings_sync

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


def ms(datestr: str) -> int:
    """'YYYY-MM-DD' → 该日 00:00 UTC 的 epoch 毫秒。"""
    y, m, d = map(int, datestr.split("-"))
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def make_fund_db(fills):
    """fills: list of (symbol, position_side, side, qty, price, 'YYYY-MM-DD')。返回临时路径。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE binance_fills "
                 "(symbol TEXT, position_side TEXT, side TEXT, qty REAL, price REAL, fill_time INTEGER)")
    conn.executemany(
        "INSERT INTO binance_fills VALUES (?,?,?,?,?,?)",
        [(s, ps, sd, q, pr, ms(d)) for s, ps, sd, q, pr, d in fills])
    conn.commit()
    conn.close()
    return path


def rnd_conn(rows):
    """rows: list of (symbol, 'YYYY-MM-DD')。内存 rnd 库，供 snap 查询。"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE rnd_indicators (symbol TEXT, date TEXT)")
    c.executemany("INSERT INTO rnd_indicators VALUES (?,?)", rows)
    c.commit()
    return c


# 各 ticker 都给一串交易日（含 07-24 周五，无 07-25/26 周末）
TRADING_DAYS = [(t, d) for t in ("GOOGL", "MU", "INTC")
                for d in ("2026-05-14", "2026-06-23", "2026-07-08", "2026-07-24")]


def test_single_open():
    print("\n[单笔开仓]")
    db = make_fund_db([("GOOGLUSDT", "LONG", "BUY", 0.26, 300.0, "2026-05-14")])
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("GOOGL", {})
    check("GOOGL 在结果里", "GOOGL" in res, list(res))
    check("open_date = 开仓当日", e.get("open_date") == "2026-05-14", e.get("open_date"))
    check("entry_price = 成交价", e.get("entry_price") == 300.0, e.get("entry_price"))
    check("rnd_date snap 到当日(有曲线)", e.get("rnd_date") == "2026-05-14", e.get("rnd_date"))


def test_scale_in():
    print("\n[加仓：open_date 取首次 0→非0]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 0.05, 100.0, "2026-06-23"),
        ("MUUSDT", "LONG", "BUY", 0.08, 110.0, "2026-07-08")])
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    check("open_date = 首笔", res.get("MU", {}).get("open_date") == "2026-06-23",
          res.get("MU", {}).get("open_date"))


def test_reopen():
    print("\n[平掉再开：open_date 取最近一次]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 0.10, 100.0, "2026-05-14"),
        ("MUUSDT", "LONG", "SELL", 0.10, 105.0, "2026-06-23"),   # 平回 0
        ("MUUSDT", "LONG", "BUY", 0.07, 108.0, "2026-07-08")])   # 再开
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    check("open_date = 最近一次开仓", res.get("MU", {}).get("open_date") == "2026-07-08",
          res.get("MU", {}).get("open_date"))


def test_closed_excluded():
    print("\n[已平仓：不出现]")
    db = make_fund_db([
        ("INTCUSDT", "LONG", "BUY", 0.3, 30.0, "2026-05-14"),
        ("INTCUSDT", "LONG", "SELL", 0.3, 32.0, "2026-07-08")])
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    check("已平仓标的不在结果里", "INTC" not in res, list(res))


def test_snap_weekend():
    print("\n[周末开仓：snap 到最近 ≤ 交易日]")
    db = make_fund_db([("GOOGLUSDT", "LONG", "BUY", 0.2, 320.0, "2026-07-25")])  # 周六
    c = rnd_conn(TRADING_DAYS)  # 有 07-24 周五、无 07-25
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("GOOGL", {})
    check("open_date 保留真实开仓(周六)", e.get("open_date") == "2026-07-25", e.get("open_date"))
    check("rnd_date snap 到周五", e.get("rnd_date") == "2026-07-24", e.get("rnd_date"))


def test_snap_before_data():
    print("\n[开仓早于 RND 数据：rnd_date=None]")
    db = make_fund_db([("GOOGLUSDT", "LONG", "BUY", 0.2, 100.0, "2026-01-01")])
    c = rnd_conn(TRADING_DAYS)  # 最早 05-14
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("GOOGL", {})
    check("open_date 仍派生", e.get("open_date") == "2026-01-01", e.get("open_date"))
    check("rnd_date = None(无更早曲线)", e.get("rnd_date") is None, repr(e.get("rnd_date")))


def test_blacklist_filtered():
    print("\n[黑名单/非美股：不出现]")
    db = make_fund_db([("SPCXUSDT", "LONG", "BUY", 0.25, 50.0, "2026-06-23")])
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    check("黑名单标的被过滤", res == {}, list(res))


def test_missing_db():
    print("\n[fund.db 缺失：{}]")
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path="/nonexistent/fund.db")
    check("返回空 dict", res == {}, list(res))


for fn in (test_single_open, test_scale_in, test_reopen, test_closed_excluded,
           test_snap_weekend, test_snap_before_data, test_blacklist_filtered, test_missing_db):
    fn()

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

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
    """fills: list of (symbol, position_side, side, qty, price, 'YYYY-MM-DD'[, trade_id])。
    返回临时路径。

    quote_qty 按 qty*price 算（与币安一致），均价即按它加权。trade_id 省略时按
    列表顺序编号（= 成交顺序）；显式给出可模拟「同一毫秒、插入顺序≠成交顺序」。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE binance_fills "
                 "(binance_trade_id INTEGER, symbol TEXT, position_side TEXT, side TEXT, "
                 " qty REAL, price REAL, quote_qty REAL, fill_time INTEGER)")
    conn.executemany(
        "INSERT INTO binance_fills VALUES (?,?,?,?,?,?,?,?)",
        [(f[6] if len(f) > 6 else i + 1, f[0], f[1], f[2], f[3], f[4], f[3] * f[4], ms(f[5]))
         for i, f in enumerate(fills)])
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
    print("\n[加仓：open_date 取首次 0→非0，均价按 quote 加权]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 0.05, 100.0, "2026-06-23"),
        ("MUUSDT", "LONG", "BUY", 0.08, 110.0, "2026-07-08")])
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    check("open_date = 首笔", e.get("open_date") == "2026-06-23", e.get("open_date"))
    # (0.05*100 + 0.08*110) / 0.13 = 13.8/0.13 = 106.153…
    check("均价 = quote 加权（非首笔价）", abs(e.get("entry_price", 0) - 106.1538) < 1e-3,
          e.get("entry_price"))
    check("记录开仓笔数", e.get("num_opening_fills") == 2, e.get("num_opening_fills"))
    check("净仓 = 累计", abs(e.get("qty", 0) - 0.13) < 1e-9, e.get("qty"))


def test_average_down():
    print("\n[摊低：低买拉低均价（用户实测场景）]")
    db = make_fund_db([
        ("NVDAUSDT", "LONG", "BUY", 1.0, 215.50, "2026-05-14"),
        ("NVDAUSDT", "LONG", "BUY", 1.0, 180.50, "2026-06-23")])
    c = rnd_conn(TRADING_DAYS + [("NVDA", d) for _, d in TRADING_DAYS])
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("NVDA", {})
    check("均价被摊低到 198.0（非首笔 215.5）", abs(e.get("entry_price", 0) - 198.0) < 1e-6,
          e.get("entry_price"))
    check("open_date 仍是持仓起始日", e.get("open_date") == "2026-05-14", e.get("open_date"))


def test_partial_close_keeps_avg():
    print("\n[部分平仓：不改均价（只减仓）]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 1.0, 100.0, "2026-05-14"),
        ("MUUSDT", "LONG", "BUY", 1.0, 80.0, "2026-06-23"),   # 均价 90
        ("MUUSDT", "LONG", "SELL", 1.0, 85.0, "2026-07-08")])  # 平一半
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    check("均价仍为 90（部分平仓不改）", abs(e.get("entry_price", 0) - 90.0) < 1e-6,
          e.get("entry_price"))
    check("净仓剩 1.0", abs(e.get("qty", 0) - 1.0) < 1e-9, e.get("qty"))
    check("open_date 仍是周期起点", e.get("open_date") == "2026-05-14", e.get("open_date"))


def test_reduce_then_add():
    print("\n[减仓后再加仓：剩余仓位 × 旧均价 + 新加仓（币安口径）]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 10.0, 100.0, "2026-05-14"),
        ("MUUSDT", "LONG", "SELL", 8.0, 120.0, "2026-06-23"),   # 止盈减仓，剩 2 @100
        ("MUUSDT", "LONG", "BUY", 8.0, 50.0, "2026-07-08")])    # 低位补仓
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    # 币安 (2×100 + 8×50)/10 = 60；已卖掉的 8 不该留在分母里（旧算法 1400/18 = 77.8）
    check("均价 = 60（已平部分不稀释新加仓）", abs(e.get("entry_price", 0) - 60.0) < 1e-6,
          e.get("entry_price"))
    check("净仓 = 10", abs(e.get("qty", 0) - 10.0) < 1e-9, e.get("qty"))
    check("open_date 仍是周期起点", e.get("open_date") == "2026-05-14", e.get("open_date"))
    check("rnd_date 仍锚周期起点", e.get("rnd_date") == "2026-05-14", e.get("rnd_date"))
    check("开仓笔数 = 2", e.get("num_opening_fills") == 2, e.get("num_opening_fills"))


def test_short_reduce_then_add():
    print("\n[空头减仓后再加仓：同口径]")
    db = make_fund_db([
        ("MUUSDT", "SHORT", "SELL", 10.0, 100.0, "2026-05-14"),
        ("MUUSDT", "SHORT", "BUY", 8.0, 80.0, "2026-06-23"),    # 回补，剩 -2 @100
        ("MUUSDT", "SHORT", "SELL", 8.0, 150.0, "2026-07-08")])  # 高位加空
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    # (2×100 + 8×150)/10 = 140
    check("空头均价 = 140", abs(e.get("entry_price", 0) - 140.0) < 1e-6, e.get("entry_price"))
    check("净仓 = -10", abs(e.get("qty", 0) + 10.0) < 1e-9, e.get("qty"))


def test_multi_round_reduce_add():
    print("\n[多轮减仓/加仓交替：逐步按剩余比例缩成本]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 4.0, 100.0, "2026-05-14"),
        ("MUUSDT", "LONG", "BUY", 6.0, 80.0, "2026-06-01"),     # 10 @88
        ("MUUSDT", "LONG", "SELL", 5.0, 95.0, "2026-06-10"),    # 5 @88
        ("MUUSDT", "LONG", "BUY", 5.0, 60.0, "2026-06-23"),     # (440+300)/10 = 10 @74
        ("MUUSDT", "LONG", "SELL", 8.0, 70.0, "2026-07-01"),    # 2 @74
        ("MUUSDT", "LONG", "BUY", 3.0, 90.0, "2026-07-08")])    # (148+270)/5 = 5 @83.6
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    check("均价 = 83.6", abs(e.get("entry_price", 0) - 83.6) < 1e-6, e.get("entry_price"))
    check("净仓 = 5", abs(e.get("qty", 0) - 5.0) < 1e-9, e.get("qty"))
    check("开仓笔数 = 4", e.get("num_opening_fills") == 4, e.get("num_opening_fills"))


def test_reduce_add_then_close_reopen():
    print("\n[减仓加仓后全平再开：旧成本不串进新周期]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 10.0, 100.0, "2026-05-14"),
        ("MUUSDT", "LONG", "SELL", 8.0, 120.0, "2026-06-01"),
        ("MUUSDT", "LONG", "BUY", 8.0, 50.0, "2026-06-10"),
        ("MUUSDT", "LONG", "SELL", 10.0, 70.0, "2026-06-23"),   # 全平
        ("MUUSDT", "LONG", "BUY", 3.0, 70.0, "2026-07-08")])    # 新周期
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    check("均价 = 新周期的 70", abs(e.get("entry_price", 0) - 70.0) < 1e-6, e.get("entry_price"))
    check("open_date = 新周期起点", e.get("open_date") == "2026-07-08", e.get("open_date"))
    check("开仓笔数重置为 1", e.get("num_opening_fills") == 1, e.get("num_opening_fills"))


def test_flip_after_reduce_add():
    print("\n[单向持仓跨零翻向：新周期只含翻过去的那部分]")
    db = make_fund_db([
        ("MUUSDT", "BOTH", "BUY", 10.0, 100.0, "2026-05-14"),
        ("MUUSDT", "BOTH", "SELL", 8.0, 110.0, "2026-06-01"),
        ("MUUSDT", "BOTH", "BUY", 8.0, 50.0, "2026-06-10"),     # +10 @60
        ("MUUSDT", "BOTH", "SELL", 15.0, 70.0, "2026-06-23"),   # 平 10、翻出 -5 @70
        ("MUUSDT", "BOTH", "SELL", 5.0, 80.0, "2026-07-08")])   # 加空 → -10 @75
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    check("翻向后均价 = 75（只含翻过去的 5@70 + 加空 5@80）",
          abs(e.get("entry_price", 0) - 75.0) < 1e-6, e.get("entry_price"))
    check("净仓 = -10", abs(e.get("qty", 0) + 10.0) < 1e-9, e.get("qty"))
    check("open_date = 翻向那笔", e.get("open_date") == "2026-06-23", e.get("open_date"))
    check("开仓笔数 = 2（翻向笔 + 加空笔）", e.get("num_opening_fills") == 2,
          e.get("num_opening_fills"))


def test_same_ms_ordered_by_trade_id():
    print("\n[同一毫秒多笔：按 trade id 定成交顺序（不看插入顺序）]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 10.0, 100.0, "2026-05-14", 1),
        ("MUUSDT", "LONG", "BUY", 8.0, 50.0, "2026-07-08", 3),    # 先插入，但成交在后
        ("MUUSDT", "LONG", "SELL", 8.0, 120.0, "2026-07-08", 2)])  # 同毫秒，先成交
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    # 真实顺序 id1→id2(卖)→id3(买)：(2×100 + 8×50)/10 = 60；按插入顺序走会得 77.8
    check("均价 = 60（先减仓后补仓）", abs(e.get("entry_price", 0) - 60.0) < 1e-6,
          e.get("entry_price"))


def test_reopen_resets_avg():
    print("\n[平掉再开：均价与起始日都重置]")
    db = make_fund_db([
        ("MUUSDT", "LONG", "BUY", 1.0, 100.0, "2026-05-14"),
        ("MUUSDT", "LONG", "SELL", 1.0, 105.0, "2026-06-23"),  # 平回 0
        ("MUUSDT", "LONG", "BUY", 2.0, 50.0, "2026-07-08")])   # 新周期
    c = rnd_conn(TRADING_DAYS)
    res = holdings_sync.entry_dates(c, fund_db_path=db)
    os.unlink(db)
    e = res.get("MU", {})
    check("均价 = 新周期的 50", abs(e.get("entry_price", 0) - 50.0) < 1e-6, e.get("entry_price"))
    check("open_date = 新周期起点", e.get("open_date") == "2026-07-08", e.get("open_date"))
    check("开仓笔数重置为 1", e.get("num_opening_fills") == 1, e.get("num_opening_fills"))


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


for fn in (test_single_open, test_scale_in, test_average_down, test_partial_close_keeps_avg,
           test_reduce_then_add, test_short_reduce_then_add, test_multi_round_reduce_add,
           test_reduce_add_then_close_reopen, test_flip_after_reduce_add,
           test_same_ms_ordered_by_trade_id, test_reopen_resets_avg, test_reopen, test_closed_excluded,
           test_snap_weekend, test_snap_before_data, test_blacklist_filtered, test_missing_db):
    fn()

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

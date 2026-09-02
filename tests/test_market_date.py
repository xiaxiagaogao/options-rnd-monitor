"""EOD 请求区间的"今天"必须是**美东今天**（2026-09-01 生产事故回归）。

事故：VPS 跑在 Asia/Singapore，cron 06:00 SGT 时 `dt.date.today()` 已经是美东的明天；
ThetaData 于 2026-08-31~09-01 间上线服务端校验
`INVALID_ARGUMENT: Date range contains future date; end must be before or equal to today`，
于是每个标的的 stock 拉取全挂 → 库冻在 08-28，而推送照常发陈旧报告。

纯 mock，不实拉网络：
    .venv/bin/python tests/test_market_date.py
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import datetime as dt

import pandas as pd

from rnd import fetch

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


# ---------- 1. market_today 取美东日期，不取本机日期 ----------
def test_market_today_is_eastern():
    print("\n[1] market_today 返回美东当日")
    # 事故当时的时刻：2026-09-01 22:00 UTC = 09-02 06:00 SGT（cron 时点）= 09-01 18:00 ET
    cron_instant = dt.datetime(2026, 9, 1, 22, 0, tzinfo=dt.timezone.utc)
    got = fetch.market_today(cron_instant)
    check("cron 时点取美东 09-01（而非新加坡的 09-02）", got == dt.date(2026, 9, 1), f"got={got}")

    # 美东与 UTC 同日的普通时刻，结果不受影响
    midday = dt.datetime(2026, 9, 1, 16, 0, tzinfo=dt.timezone.utc)   # ET 12:00
    check("美东盘中时刻正常", fetch.market_today(midday) == dt.date(2026, 9, 1))

    # 不传参数时用当前时刻，且永远不会超前于本机日期 +1
    now = fetch.market_today()
    check("无参调用返回 date", isinstance(now, dt.date), f"now={now}")


# ---------- 2. chunked 永不把未来日期发给 ThetaData ----------
def test_chunked_clamps_future_end():
    print("\n[2] stock_history_eod_chunked 钳住未来 end_date")
    sent = []

    def fake_eod(symbol, start_date, end_date):
        sent.append((start_date, end_date))
        return pd.DataFrame({"created": [pd.Timestamp(end_date)], "close": [100.0]})

    with mock.patch.object(fetch, "_client") as mc, \
         mock.patch.object(fetch, "market_today", return_value=dt.date(2026, 9, 1)):
        mc.return_value.stock_history_eod.side_effect = fake_eod
        # 事故复现：eod_update 传的是 SGT 的今天 09-02
        fetch.stock_history_eod_chunked("SPY", dt.date(2026, 8, 29), dt.date(2026, 9, 2))
    check("发出的 end_date 被钳到美东今天", sent and sent[-1][1] == dt.date(2026, 9, 1),
          f"sent={sent}")
    check("start 未被改动", sent and sent[0][0] == dt.date(2026, 8, 29), f"sent={sent}")

    # 整段都在未来（周末盘前多跑一次）→ 不发请求、返回空，由调用方按"无新交易日"处理
    sent.clear()
    with mock.patch.object(fetch, "_client") as mc, \
         mock.patch.object(fetch, "market_today", return_value=dt.date(2026, 9, 1)):
        mc.return_value.stock_history_eod.side_effect = fake_eod
        df = fetch.stock_history_eod_chunked("SPY", dt.date(2026, 9, 2), dt.date(2026, 9, 3))
    check("整段在未来：不发请求", sent == [], f"sent={sent}")
    check("整段在未来：返回空 DataFrame", df.empty, f"empty={df.empty}")

    # 历史区间不受影响
    sent.clear()
    with mock.patch.object(fetch, "_client") as mc, \
         mock.patch.object(fetch, "market_today", return_value=dt.date(2026, 9, 1)):
        mc.return_value.stock_history_eod.side_effect = fake_eod
        fetch.stock_history_eod_chunked("SPY", dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    check("历史区间原样发出", sent == [(dt.date(2026, 7, 1), dt.date(2026, 7, 31))], f"sent={sent}")


# ---------- 3. latest_trading_day 的 end_date 也不越界 ----------
def test_latest_trading_day_end():
    print("\n[3] latest_trading_day end_date ≤ 美东今天")
    sent = {}

    def fake_eod(symbol, start_date, end_date):
        sent["range"] = (start_date, end_date)
        return pd.DataFrame({"created": [pd.Timestamp("2026-09-01")], "close": [640.0]})

    with mock.patch.object(fetch, "_client") as mc, \
         mock.patch.object(fetch, "market_today", return_value=dt.date(2026, 9, 1)):
        mc.return_value.stock_history_eod.side_effect = fake_eod
        day, close = fetch.latest_trading_day("SPY")
    check("end_date = 美东今天", sent["range"][1] == dt.date(2026, 9, 1), f"sent={sent}")
    check("返回最近交易日", day == dt.date(2026, 9, 1), f"day={day}")


# ---------- 4. eod_update 的 today 来自美东 ----------
def test_eod_update_uses_market_today():
    print("\n[4] eod_update 增量窗口用美东今天")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import eod_update

    captured = {}

    def fake_chunked(symbol, start, end):
        captured["window"] = (start, end)
        return pd.DataFrame()   # 空 → update_symbol 走"无新交易日"提前返回

    conn = mock.MagicMock()
    conn.execute.return_value.fetchone.return_value = ("2026-08-28",)
    with mock.patch.object(eod_update.fetch, "stock_history_eod_chunked", fake_chunked), \
         mock.patch.object(eod_update.fetch, "market_today", return_value=dt.date(2026, 9, 1)):
        eod_update.update_symbol(conn, "SPY", pd.Series(dtype=float), fetch.market_today())
    check("增量窗口 end 不越过美东今天",
          captured.get("window", (None, None))[1] <= dt.date(2026, 9, 1),
          f"window={captured.get('window')}")


for fn in (test_market_today_is_eastern, test_chunked_clamps_future_end,
           test_latest_trading_day_end, test_eod_update_uses_market_today):
    fn()

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

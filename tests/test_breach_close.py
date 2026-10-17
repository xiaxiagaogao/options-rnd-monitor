"""今日页持仓灯 / 标的页详情的「收盘越线」改按最新收盘判。临时库 + 合成 fund.db，无网络。

    .venv/bin/python tests/test_breach_close.py

回归点（2026-10-16 RKLB）：单标的 RND 停更时，灯按 RND 最新日那天的收盘判，
停更后真实跌破看不见。改为取 raw_chain 在 [RND 最新日, 全池最新数据日] 内的最新收盘，
并把收盘日带出来；晚于全池数据日的链（拉完未算完的中间态）不读。
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np                              # noqa: E402
from scipy.stats import norm                    # noqa: E402

from rnd import db, holdings_sync               # noqa: E402
from server import queries as q                 # noqa: E402

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


# 数字全是合成的。F=100、σ1=5 → Q05 ≈ 91.78。入场 08-04 → 入场日 Q05 取 08-04 行。
DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"]
EXP = "2026-08-21"
F, SIG = 100.0, 5.0
STALE_LAST = 3                                    # STL 的 RND 只到 08-06（D-2）
CLOSE = {"STL": [100.0, 100.0, 100.0, 99.0, 95.0, 88.0],   # D-2 在 Q05 上，D 跌破
         "FRS": [100.0, 100.0, 100.0, 99.0, 95.0, 94.0]}   # 一直在 Q05 上
AFTER_CAP = ("2026-08-11", 200.0)                 # 晚于全池数据日；若被读到，STL 的灯会翻回不破

tmp = Path(tempfile.mkdtemp()) / "breach.sqlite"
c0 = db.get_conn(tmp)


def add_day(sym, d):
    s = F * np.exp(np.linspace(-0.72, 0.72, 801))
    cdf = norm.cdf((np.log(s / F) + 0.5 * 0.12**2) / 0.12)
    db.upsert_curve(c0, d, sym, EXP, {"strikes": s.tolist(), "density": np.gradient(cdf, s).tolist(),
                                      "cdf": cdf.tolist()}, {"x_quoted_range": [-0.2, 0.2]})
    qs = {"q05": F - 1.645 * SIG, "q25": F - 0.674 * SIG, "q50": F,
          "q75": F + 0.674 * SIG, "q95": F + 1.645 * SIG}
    db.upsert_indicators(c0, {
        "date": d, "symbol": sym, "expiry": EXP, "dte": 30, "forward": F,
        "sigma1_abs": SIG, "sigma1_pct": SIG / F, **qs,
        **{f"{k}_in_range": 1 for k in qs}, "mode": F, "n_modes": 1, "modes_json": "[]",
        "gate_pass": 1, "gate_detail": "{}", "pinned": 1, "roll": 0})


for sym in ("STL", "FRS"):
    for i, d in enumerate(DATES):
        if sym == "FRS" or i <= STALE_LAST:
            add_day(sym, d)
        db.insert_raw_chain(c0, [(d, sym, EXP, 100.0, "C", 1, 1.1, 0, None, "", CLOSE[sym][i], 0.04)])
db.insert_raw_chain(c0, [(AFTER_CAP[0], "STL", EXP, 100.0, "C", 1, 1.1, 0, None, "", AFTER_CAP[1], 0.04)])
c0.close()
q.conn = lambda: db.get_conn(tmp)
q.get_symbols = lambda: ["STL", "FRS"]


def ms(datestr: str) -> int:
    y, m, d = map(int, datestr.split("-"))
    return int(datetime(y, m, d, 15, tzinfo=timezone.utc).timestamp() * 1000)


fd, FUND = tempfile.mkstemp(suffix=".db")
os.close(fd)
fc = sqlite3.connect(FUND)
fc.execute("CREATE TABLE binance_fills (binance_trade_id INTEGER, symbol TEXT, "
           "position_side TEXT, side TEXT, qty REAL, price REAL, quote_qty REAL, fill_time INTEGER)")
fc.executemany("INSERT INTO binance_fills VALUES (?,?,?,?,?,?,?,?)",
               [(1, "STLUSDT", "LONG", "BUY", 1.0, 100.0, 100.0, ms("2026-08-04")),
                (2, "FRSUSDT", "LONG", "BUY", 1.0, 100.0, 100.0, ms("2026-08-04"))])
fc.commit()
fc.close()
_real_entry_dates = holdings_sync.entry_dates
holdings_sync.entry_dates = lambda conn, *a, **k: _real_entry_dates(conn, FUND)   # 页面走默认路径参数
Q05 = F - 1.645 * SIG


def test_overview_light():
    print("\n[今日页灯] RND 停在 D-2、D 日收盘跌破入场日 Q05 → 灯亮，收盘日报 D")
    check("前提：D-2 收盘在 Q05 上、D 日跌破", CLOSE["STL"][STALE_LAST] > Q05 > CLOSE["STL"][-1])
    hs = {h["symbol"]: h for h in q.overview()["holdings"]}
    s = hs.get("STL", {})
    check("STL breached == True", s.get("breached") is True, f"got={s.get('breached')}")
    check("STL 收盘取 D 日（不读晚于全池数据日的链）",
          s.get("close") == CLOSE["STL"][-1] and s.get("close_date") == DATES[-1],
          f"got={s.get('close')} @ {s.get('close_date')}")
    f = hs.get("FRS", {})
    check("FRS（不陈旧）breached == False、收盘日 D",
          f.get("breached") is False and f.get("close_date") == DATES[-1],
          f"got={f.get('breached')} @ {f.get('close_date')}")


def test_symbol_detail_close():
    print("\n[标的页] 详情带最新收盘与收盘日，前端两盏灯用它判，不再取 RND 最新日的收盘")
    d = q.symbol_detail("STL")
    check("RND 读数日仍是 08-06", d.get("date") == DATES[STALE_LAST], f"got={d.get('date')}")
    check("close = D 日收盘、close_date = D",
          d.get("close") == CLOSE["STL"][-1] and d.get("close_date") == DATES[-1],
          f"got={d.get('close')} @ {d.get('close_date')}")
    df = q.symbol_detail("FRS")
    check("FRS close_date = RND 读数日 = D",
          df.get("close_date") == df.get("date") == DATES[-1], f"got={df.get('close_date')} / {df.get('date')}")


def test_latest_close_bounds():
    print("\n[latest_close] 区间 [asof, cap]")
    c = q.conn()
    try:
        check("cap = 全池数据日 → D", q.latest_close(c, "STL", DATES[STALE_LAST], DATES[-1])
              == (CLOSE["STL"][-1], DATES[-1]))
        check("历史回放 cap = asof → 只读当天", q.latest_close(c, "STL", DATES[STALE_LAST], DATES[STALE_LAST])
              == (CLOSE["STL"][STALE_LAST], DATES[STALE_LAST]))
        check("asof 为 None（无 RND）→ 不查", q.latest_close(c, "STL", None, DATES[-1]) == (None, None))
    finally:
        c.close()


if __name__ == "__main__":
    test_overview_light()
    test_symbol_detail_close()
    test_latest_close_bounds()
    if _failed:
        print(f"\n{len(_failed)} 项失败: {_failed}")
        sys.exit(1)
    print("\n全部通过。")

"""研究助手 / 市场展望的持仓上下文改读币安实际持仓。临时库 + 合成 fund.db，无网络。

    .venv/bin/python tests/test_assistant_holdings.py

回归点：trade_journal 为空、但币安有持仓时，上下文必须带上持仓（此前一律 open=False，
展望整段写成「全组无持仓」）。冻结口径 = 本周期冻结，与止盈止损页曲线一同一套数。
"""
import json
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
from server import assistant, queries as q      # noqa: E402

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


# 两个周期：前三天钉 EXP_A，第四天 roll 到 EXP_B。数字全是合成的。
DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"]
EXP_A, EXP_B = "2026-08-21", "2026-09-18"
FWD = [100.0, 101.0, 99.0, 102.0, 103.0, 104.0]
SIG = [5.0, 5.1, 5.2, 6.0, 6.1, 6.2]
CLOSE = [99.8, 100.7, 98.9, 101.8, 102.9, 96.0]   # 最新收盘压到本周期冻结 Q25(≈97.96) 之下、Q05(≈92.13) 之上

tmp = Path(tempfile.mkdtemp()) / "asst.sqlite"
c0 = db.get_conn(tmp)


def grid_for(F, v=0.12):
    s = F * np.exp(np.linspace(-6 * v, 6 * v, 801))
    cdf = norm.cdf((np.log(s / F) + 0.5 * v**2) / v)
    return {"strikes": s.tolist(), "density": np.gradient(cdf, s).tolist(), "cdf": cdf.tolist()}


def add_day(sym, i, expiry, pinned, roll=0):
    d, F, sig = DATES[i], FWD[i], SIG[i]
    g = grid_for(F)
    db.upsert_curve(c0, d, sym, expiry, g, {"x_quoted_range": [-0.2, 0.2]})
    qs = {"q05": F - 1.645 * sig, "q25": F - 0.674 * sig, "q50": F,
          "q75": F + 0.674 * sig, "q95": F + 1.645 * sig}
    db.upsert_indicators(c0, {
        "date": d, "symbol": sym, "expiry": expiry, "dte": 30, "forward": F,
        "sigma1_abs": sig, "sigma1_pct": sig / F, **qs,
        **{f"{k}_in_range": 1 for k in qs}, "mode": F, "n_modes": 1, "modes_json": "[]",
        "gate_pass": 1, "gate_detail": "{}", "pinned": pinned, "roll": roll})


for i, d in enumerate(DATES):
    cyc_b = i >= 3
    for sym in ("TST", "NEW", "OTH", "LAT"):
        add_day(sym, i, EXP_A, pinned=int(not cyc_b))
        add_day(sym, i, EXP_B, pinned=int(cyc_b), roll=int(i == 3))
        db.insert_raw_chain(c0, [(d, sym, EXP_B, 100.0, "C", 1, 1.1, 0, None, "", CLOSE[i], 0.04)])
c0.close()
q.conn = lambda: db.get_conn(tmp)


def ms(datestr: str) -> int:
    y, m, d = map(int, datestr.split("-"))
    return int(datetime(y, m, d, 15, tzinfo=timezone.utc).timestamp() * 1000)


def make_fund_db(fills):
    """fills: (symbol, position_side, side, qty, price, 'YYYY-MM-DD')。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE binance_fills (binance_trade_id INTEGER, symbol TEXT, "
                 "position_side TEXT, side TEXT, qty REAL, price REAL, quote_qty REAL, fill_time INTEGER)")
    conn.executemany("INSERT INTO binance_fills VALUES (?,?,?,?,?,?,?,?)",
                     [(i + 1, f[0], f[1], f[2], f[3], f[4], f[3] * f[4], ms(f[5]))
                      for i, f in enumerate(fills)])
    conn.commit()
    conn.close()
    return path


# TST 在上一周期（EXP_A）开仓 → 冻结日 = 本周期第一天 08-06；
# NEW 在本周期内开仓 → 冻结日 = 入场日 08-07；OTH 无币安持仓；
# LAT 在最新数据日之后才开（美股收盘后、cron 之前开仓，UTC 日历日已跨到次日）。
FUND = make_fund_db([
    ("TSTUSDT", "LONG", "BUY", 2.0, 100.0, "2026-08-04"),
    ("NEWUSDT", "LONG", "BUY", 1.0, 103.0, "2026-08-07"),
    ("LATUSDT", "LONG", "BUY", 1.0, 98.0, "2026-08-11"),
])
holdings_sync.FUND_DB_PATH = FUND


def ind_on(sym, date):
    c = q.conn()
    try:
        return q._row(c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
                      (sym, date))
    finally:
        c.close()


def test_binance_position_in_context():
    print("\n[持仓源] journal 为空、币安有持仓 → 上下文带持仓")
    c = q.conn()
    check("前提：journal 无开仓", q.open_positions(c) == [])
    c.close()
    p = assistant.build_context("TST")["position"]
    check("open == True", p.get("open") is True, f"got={p.get('open')}")
    check("source == binance", p.get("source") == "binance", f"got={p.get('source')}")
    check("direction == long", p.get("direction") == "long", f"got={p.get('direction')}")
    check("entry_price / qty / open_date 来自币安",
          p.get("entry_price") == 100.0 and p.get("qty") == 2.0
          and p.get("open_date") == "2026-08-04", f"got={p.get('entry_price')}/{p.get('qty')}/{p.get('open_date')}")
    check("无币安持仓的标的 open == False",
          assistant.build_context("OTH")["position"].get("open") is False)


def test_identity_unregistered():
    print("\n[身份] 币安仓位没登记投机/信念 → 两条线都给，注明身份未登记")
    p = assistant.build_context("TST")["position"]
    check("identity 为 None（不替用户假设）", p.get("identity") is None, f"got={p.get('identity')}")
    check("stop_q 为 None", p.get("stop_q") is None, f"got={p.get('stop_q')}")
    check("注明「身份未登记」", "身份未登记" in json.dumps(p, ensure_ascii=False))
    lines = p.get("stop_lines") or {}
    check("两条线：投机 Q25 + 信念 Q05",
          (lines.get("speculative") or {}).get("quantile") == "q25"
          and (lines.get("conviction") or {}).get("quantile") == "q05", f"got={lines}")


def test_cycle_freeze():
    print("\n[冻结口径] 本周期冻结：上周期开仓 → 冻结在本周期第一天，不用入场日")
    p = assistant.build_context("TST")["position"]
    fz = p.get("frozen") or {}
    fr = ind_on("TST", "2026-08-06")
    check("冻结日 = 本周期第一天 08-06", fz.get("frozen_date") == "2026-08-06", f"got={fz.get('frozen_date')}")
    check("冻结到期 = 当前钉住到期 EXP_B", fz.get("frozen_expiry") == EXP_B, f"got={fz.get('frozen_expiry')}")
    check("冻结分位取自 08-06 行", fz.get("frozen_q05") == fr["q05"] and fz.get("frozen_q25") == fr["q25"])
    check("口径写明「本周期冻结」", "本周期冻结" in (p.get("frozen_basis") or ""), f"got={p.get('frozen_basis')}")
    lines = p["stop_lines"]
    check("投机线 = 冻结 Q25", lines["speculative"]["level"] == fr["q25"])
    check("信念线 = 冻结 Q05", lines["conviction"]["level"] == fr["q05"])
    close = CLOSE[-1]
    check("收盘确认：投机线判定按 close < Q25",
          lines["speculative"]["invalidated"] is (close < fr["q25"]))
    check("收盘确认：信念线判定按 close < Q05",
          lines["conviction"]["invalidated"] is (close < fr["q05"]))
    cur = ind_on("TST", DATES[-1])
    exp_q50 = (cur["q50"] - fr["q50"]) / fr["sigma1_abs"]
    check("offset.q50 = (现Q50−冻结Q50)/冻结σ1",
          abs((p.get("offset") or {}).get("q50", 1e9) - exp_q50) < 1e-9,
          f"got={(p.get('offset') or {}).get('q50')} expect={exp_q50}")

    check("冻结日闸门通过 → 口径里不提闸门", "闸门" not in p["frozen_basis"])

    print("\n[冻结口径] 本周期内开仓 → 冻结在入场日")
    pn = assistant.build_context("NEW")["position"]
    check("冻结日 = 入场日 08-07", (pn.get("frozen") or {}).get("frozen_date") == "2026-08-07",
          f"got={(pn.get('frozen') or {}).get('frozen_date')}")

    print("\n[冻结口径] 冻结日闸门未过 → 照给线，口径里注明")
    c = q.conn()
    c.execute("UPDATE rnd_indicators SET gate_pass=0 WHERE symbol='NEW' AND date='2026-08-07' AND pinned=1")
    c.commit()
    try:
        pg = assistant.build_context("NEW")["position"]
    finally:
        c.execute("UPDATE rnd_indicators SET gate_pass=1 WHERE symbol='NEW' AND date='2026-08-07' AND pinned=1")
        c.commit()
        c.close()
    check("frozen_gate_pass == False", pg["frozen"]["frozen_gate_pass"] is False)
    check("口径注明「冻结日闸门未过」", "冻结日闸门未过" in pg["frozen_basis"], f"got={pg['frozen_basis']}")
    check("两条线照给", pg["stop_lines"]["conviction"]["level"] is not None)


def test_matches_exit_page():
    print("\n[与页面一致] 冻结日 / Q05 / Q25 与止盈止损页曲线一同一套数")
    real = holdings_sync.entry_dates       # 页面走默认路径参数（导入时绑定），这里指到合成库
    holdings_sync.entry_dates = lambda conn, *a, **k: real(conn, FUND)
    try:
        for sym in ("TST", "NEW"):
            cost = q.exit_curves(sym)["curves"]["cost"]
            fz = assistant.build_context(sym)["position"]["frozen"]
            check(f"{sym} 冻结日一致", cost["date"] == fz["frozen_date"],
                  f"page={cost['date']} ctx={fz['frozen_date']}")
            check(f"{sym} Q05/Q25 一致",
                  cost["q05"] == fz["frozen_q05"] and cost["q25"] == fz["frozen_q25"])
    finally:
        holdings_sync.entry_dates = real


def test_close_breach_threshold():
    print("\n[失效判定] 最新收盘压在 Q25 下、Q05 上 → 投机线破、信念线未破")
    lines = assistant.build_context("TST")["position"]["stop_lines"]
    check("投机线 invalidated == True", lines["speculative"]["invalidated"] is True)
    check("信念线 invalidated == False", lines["conviction"]["invalidated"] is False)


def test_outlook_context_has_positions():
    print("\n[展望] outlook 的上下文里有持仓，不再是「全组无持仓」")
    calls = []
    real = holdings_sync.entry_dates

    def counting(conn, *a, **k):
        calls.append(1)
        return real(conn, *a, **k)

    holdings_sync.entry_dates = counting
    try:
        msg, asof = assistant.build_outlook_messages(["TST", "NEW", "OTH"])
    finally:
        holdings_sync.entry_dates = real
    user = msg["user"]
    packs = json.loads(user.split("```json\n", 1)[1].split("\n```", 1)[0])
    opened = {s: packs[s]["position"].get("open") for s in packs}
    check("TST / NEW 有持仓、OTH 无", opened == {"TST": True, "NEW": True, "OTH": False}, f"got={opened}")
    check("不是全组无持仓", any(v is True for v in opened.values()))
    check("提示里不含「全组无持仓」", "全组无持仓" not in user)
    check("提示交代身份未登记的写法", "身份未登记" in user)
    check("提示交代冻结口径", "本周期冻结" in user)
    check("币安持仓只读一次（不按标的重复读 fund.db）", len(calls) == 1, f"calls={len(calls)}")


def test_fund_db_unavailable():
    print("\n[降级] fund.db 读不到 → 持仓「未知」，不能当成无持仓")
    holdings_sync.FUND_DB_PATH = "/nonexistent/fund.db"
    try:
        p = assistant.build_context("TST")["position"]
    finally:
        holdings_sync.FUND_DB_PATH = FUND
    check("open 为 None（未知），不是 False", p.get("open") is None, f"got={p.get('open')}")
    check("带原因", bool(p.get("reason")), f"got={p.get('reason')}")


def test_asof_before_entry():
    print("\n[历史 asof] 数据日早于开仓日 → 当时不在仓")
    p = assistant.build_context("NEW", asof="2026-08-05")["position"]
    check("open == False", p.get("open") is False, f"got={p.get('open')}")

    print("\n[最新 asof] 开仓日（UTC）晚于最新数据日 → 仍在仓，冻结在最新数据日（同止盈止损页）")
    p = assistant.build_context("LAT")["position"]
    check("open == True", p.get("open") is True, f"got={p.get('open')} note={p.get('note')}")
    check("冻结日 = 最新数据日 08-10", (p.get("frozen") or {}).get("frozen_date") == "2026-08-10",
          f"got={(p.get('frozen') or {}).get('frozen_date')}")


def test_journal_still_wins():
    print("\n[journal 轨] 有 journal 开仓时照旧走 journal（与标的页卡片同优先级）")
    r = q.journal_open({"symbol": "OTH", "identity": "conviction", "entry_price": 100.0})
    check("前提：journal 开仓成功", r.get("ok") is True, f"got={r}")
    p = assistant.build_context("OTH")["position"]
    check("source == journal", p.get("source") == "journal", f"got={p.get('source')}")
    check("identity == conviction", p.get("identity") == "conviction")
    check("stop_q == q05", p.get("stop_q") == "q05")


if __name__ == "__main__":
    test_binance_position_in_context()
    test_identity_unregistered()
    test_cycle_freeze()
    test_matches_exit_page()
    test_close_breach_threshold()
    test_outlook_context_has_positions()
    test_fund_db_unavailable()
    test_asof_before_entry()
    test_journal_still_wins()
    if _failed:
        print(f"\n{len(_failed)} 项失败: {_failed}")
        sys.exit(1)
    print("\n全部通过。")

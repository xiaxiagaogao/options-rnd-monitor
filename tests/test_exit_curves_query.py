"""止盈止损装配层 + 路由（exit-curves spec §2 / §6）。临时库 + 合成分布，无网络。

    .venv/bin/python tests/test_exit_curves_query.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["SESSION_SECRET"] = "test-secret-for-regression"
os.environ["DASHBOARD_PASSWORD"] = "test-password"
os.environ["COOKIE_SECURE"] = "0"

import numpy as np                              # noqa: E402
from scipy.stats import norm                    # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

from rnd import db, holdings_sync               # noqa: E402
from server import api, queries as q            # noqa: E402

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07", "2026-08-10"]
EXP_A, EXP_B = "2026-08-21", "2026-09-18"      # d1–d3 钉 A，d4 起 roll 到 B
FWD = [100.0, 101.0, 99.0, 102.0, 103.0, 104.0]
CLOSE = [99.8, 100.7, 98.9, 101.8, 102.9, 103.7]

tmp = Path(tempfile.mkdtemp()) / "exit.sqlite"
c0 = db.get_conn(tmp)


def grid_for(F, v=0.12):
    s = F * np.exp(np.linspace(-6 * v, 6 * v, 801))
    cdf = norm.cdf((np.log(s / F) + 0.5 * v**2) / v)
    return {"strikes": s.tolist(), "density": np.gradient(cdf, s).tolist(), "cdf": cdf.tolist()}


def add_day(sym, d, expiry, F, pinned, roll=0, gate=1):
    g = grid_for(F)
    db.upsert_curve(c0, d, sym, expiry, g, {"x_quoted_range": [-0.2, 0.2]})
    s, cdf = np.asarray(g["strikes"]), np.asarray(g["cdf"])
    qv = lambda p: float(np.interp(p, cdf, s))
    db.upsert_indicators(c0, {"date": d, "symbol": sym, "expiry": expiry, "dte": 30,
                              "forward": F, "sigma1_pct": 0.12, "q05": qv(0.05), "q25": qv(0.25),
                              "gate_pass": gate, "pinned": pinned, "roll": roll})


for i, d in enumerate(DATES):
    pinned_b = i >= 3
    add_day("TST", d, EXP_A, FWD[i], pinned=int(not pinned_b))
    add_day("TST", d, EXP_B, FWD[i], pinned=int(pinned_b), roll=int(i == 3),
            gate=0 if d == "2026-08-06" else 1)       # 本周期首日闸门 FAIL：照画但带标记
    add_day("OTH", d, EXP_B, 50.0, pinned=1)
    db.insert_raw_chain(c0, [(d, "TST", EXP_B, 100.0, "C", 1, 1.1, 0, None, "", CLOSE[i], 0.04),
                             (d, "OTH", EXP_B, 50.0, "C", 1, 1.1, 0, None, "", 49.9, 0.04)])
c0.close()

ENTRIES = {}
q.conn = lambda: db.get_conn(tmp)
q.get_symbols = lambda: ["TST", "OTH", "EMPTY"]
holdings_sync.entry_dates = lambda conn, *a, **k: ENTRIES


def entry(rnd_date, price, qty):
    return {"open_date": rnd_date, "rnd_date": rnd_date, "entry_price": price, "qty": qty,
            "num_opening_fills": 1}


def test_cycle_start():
    print("\n[周期] 入场早于本周期 → 冻结在本周期第一天（跳过非 pinned 的同到期行）")
    ENTRIES.clear()
    ENTRIES["TST"] = entry("2026-08-04", 110.0, 2.0)
    r = q.exit_curves("TST")
    cost = r["curves"]["cost"]
    check("冻结日 = 08-06（到期 B 的第一个 pinned 日）", cost["date"] == "2026-08-06", cost["date"])
    check("cycle_start = 08-06", cost["cycle_start"] == "2026-08-06")
    check("frozen_reason = cycle_start", cost["frozen_reason"] == "cycle_start")
    check("冻结日闸门 FAIL 如实带出", cost["gate_pass"] is False)
    check("成本曲线 S0 = 冻结日 forward", cost["s0"] == 102.0)
    check("成本曲线锚点 = 成本价", cost["anchor"] == 110.0)
    today = r["curves"]["today"]
    check("当日曲线 = 最新数据日", today["date"] == "2026-08-10" and r["date"] == "2026-08-10")
    check("当日曲线锚点 = 当日收盘", today["anchor"] == 103.7 and r["close"] == 103.7)
    check("两条曲线同到期", cost["expiry"] == today["expiry"] == EXP_B)
    lo, hi = r["axis"]
    check("价位轴包含成本与收盘", lo < 103.7 < hi and lo < 110.0 < hi, f"[{lo:.1f}, {hi:.1f}]")
    check("两条曲线共用价位轴", cost["down"]["prices"][0] == today["down"]["prices"][0])


def test_entry_in_cycle():
    print("\n[周期] 入场在本周期内 → 冻结在入场日")
    ENTRIES.clear()
    ENTRIES["TST"] = entry("2026-08-07", 103.0, 1.0)
    cost = q.exit_curves("TST")["curves"]["cost"]
    check("冻结日 = 入场日 08-07", cost["date"] == "2026-08-07", cost["date"])
    check("frozen_reason = entry", cost["frozen_reason"] == "entry")


def test_deep_underwater_and_short():
    print("\n[锚点] 深套多头：价位轴拉到成本；空头：盈亏按锚点判、不按 S0")
    ENTRIES.clear()
    ENTRIES["TST"] = entry("2026-08-04", 200.0, 1.0)
    r = q.exit_curves("TST")
    check("价位轴上端 > 成本 200", r["axis"][1] > 200.0, f"{r['axis']}")
    ENTRIES["TST"] = entry("2026-08-04", 104.0, -3.0)
    today = q.exit_curves("TST")["curves"]["today"]
    # 盈亏按锚点（当日收盘 103.7）判，不按 S0（forward 104.0）：两者之间的价位
    # 在下方分支里，但空头在那里平仓仍是亏的
    pairs = [(k, v) for side in ("down", "up")
             for k, v in zip(today[side]["prices"], today[side]["locked"])]
    check("空头：锚点以下锁定 > 0", all(v > 0 for k, v in pairs if k < 103.7))
    check("空头：锚点以上锁定 < 0", all(v < 0 for k, v in pairs if k > 103.7))
    check("空头：锚点与 S0 之间（下方分支）为负",
          any(v < 0 for k, v in zip(today["down"]["prices"], today["down"]["locked"])
              if 103.7 < k < 104.0))


def test_no_position_and_json():
    print("\n[无持仓] 只有当日曲线、无锁定金额；输出可严格 JSON 序列化")
    ENTRIES.clear()
    r = q.exit_curves("TST")
    check("无 cost 曲线", "cost" not in r["curves"] and r["position"] is None)
    check("locked 为 None", r["curves"]["today"]["up"]["locked"] is None)
    try:
        json.dumps(r, allow_nan=False)
        ok = True
    except ValueError:
        ok = False
    check("无 NaN / inf", ok)
    check("无数据标的 → ok=False", q.exit_curves("EMPTY")["ok"] is False)


def test_list():
    print("\n[列表] 持仓在前、池内其他在后；无数据标的不列")
    ENTRIES.clear()
    ENTRIES["TST"] = entry("2026-08-04", 110.0, 2.0)
    r = q.exit_curves_list()
    check("holdings = [TST]", [x["symbol"] for x in r["holdings"]] == ["TST"])
    check("others = [OTH]", [x["symbol"] for x in r["others"]] == ["OTH"])
    h = r["holdings"][0]
    check("持仓条目带成本 / 数量 / 收盘", h["entry_price"] == 110.0 and h["qty"] == 2.0
          and h["close"] == 103.7)


def test_auth():
    print("\n[鉴权] 两个新路由未登录一律 401")
    client = TestClient(api.app)
    for url in ("/api/exit-curves", "/api/symbol/TST/exit-curves"):
        check(f"{url} → 401", client.get(url).status_code == 401)


if __name__ == "__main__":
    for t in (test_cycle_start, test_entry_in_cycle, test_deep_underwater_and_short,
              test_no_position_and_json, test_list, test_auth):
        t()
    print(f"\n{'ALL PASS' if not _failed else 'FAILED: ' + ', '.join(_failed)}")
    sys.exit(1 if _failed else 0)

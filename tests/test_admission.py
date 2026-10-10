"""准入阈值回归（2026-07-27 点差 3%→10%；2026-10-10 双边档数 15→10 校准）。

纯合成 chain，不实拉网络：验 _metrics_from_chain 的度量 + 阈值常量语义，
以及 check_candidate 在假 fetch 下的整体判定。
    .venv/bin/python tests/test_admission.py
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from rnd import admission, fetch

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


def make_chain(close: float, rel_spread: float, n_strikes: int) -> pd.DataFrame:
    """构造 n_strikes 个双边行权价、每个相对点差恒为 rel_spread 的合成链。"""
    lo = -(n_strikes // 2)
    strikes = [close * (1 + i * 0.01) for i in range(lo, lo + n_strikes)]
    rows = []
    for k in strikes:
        mid = max(k * 0.05, 1.0)
        half = mid * rel_spread / 2
        rows.append({"strike": k, "right": "C", "bid": mid - half, "ask": mid + half})
    return pd.DataFrame(rows)


def verdict(rel_spread, n_strikes, close=100.0):
    m = admission._metrics_from_chain(make_chain(close, rel_spread, n_strikes), close)
    spread_ok = m["atm_spread_med"] is not None and m["atm_spread_med"] <= admission.MAX_ATM_SPREAD
    cov_ok = m["n_two_sided_strikes"] >= admission.MIN_TWO_SIDED
    return spread_ok and cov_ok, m


print("[常量] 07-27 / 10-10 校准值")
check("MAX_ATM_SPREAD = 0.10", admission.MAX_ATM_SPREAD == 0.10, str(admission.MAX_ATM_SPREAD))
check("MIN_TWO_SIDED = 10", admission.MIN_TWO_SIDED == 10, str(admission.MIN_TWO_SIDED))

print("[点差边界] 覆盖充足（40 行权价）")
v, m = verdict(0.058, 40)   # GOOGL 档
check("5.8% 点差通过（旧 3% 会拒，回归防倒退）", v, f"med={m['atm_spread_med']:.3f}")
v, m = verdict(0.034, 40)   # MU 档
check("3.4% 点差通过", v, f"med={m['atm_spread_med']:.3f}")
v, m = verdict(0.108, 40)   # HOOD 档
check("10.8% 点差被拒", not v, f"med={m['atm_spread_med']:.3f}")
v, m = verdict(0.153, 40)   # RKLB 档
check("15.3% 点差被拒", not v, f"med={m['atm_spread_med']:.3f}")

print("[覆盖边界] 点差达标但行权价太少")
v, m = verdict(0.05, 6)     # NOK/FLNC 档：点差 OK，双边仅 6
check("双边 6 个被 coverage 挡（<10）", not v, f"n_two_sided={m['n_two_sided_strikes']}")


def chain_on_grid(strikes, close, spread=0.015, one_sided=()):
    """按给定挂档造 CALL+PUT 链；mid 随 moneyness 衰减，相对点差 = spread；one_sided 内只挂 ask。"""
    rows = []
    for k in strikes:
        for right in ("CALL", "PUT"):
            mid = max(0.5, 10 - abs(k - close) * 0.3)
            bid = 0.0 if k in one_sided else mid * (1 - spread / 2)
            rows.append({"strike": k, "right": right, "bid": bid, "ask": mid * (1 + spread / 2)})
    return pd.DataFrame(rows)


def candidate(chain, close):
    """假 fetch 跑 check_candidate（不触网）。"""
    orig = (fetch.latest_trading_day, fetch.monthly_expirations, fetch.fetch_chain_eod)
    fetch.latest_trading_day = lambda s: (dt.date(2026, 10, 9), close)
    fetch.monthly_expirations = lambda s, d: [dt.date(2026, 11, 20)]
    fetch.fetch_chain_eod = lambda s, e, d: chain
    try:
        return admission.check_candidate("TEST")
    finally:
        fetch.latest_trading_day, fetch.monthly_expirations, fetch.fetch_chain_eod = orig


print("[10-10 校准] 高价股稀档不该被挡，薄链仍挡")
grid = [20.0, 23.0, 25.0, 28.0, 30.0, 33.0, 35.0, 37.0, 40.0, 42.0, 45.0, 47.0]   # INTC 10-09 实测挂档
grid += [50 + 2.5 * i for i in range(21)]                                         # 50 … 100 每 $2.5
grid += [105.0 + 5 * i for i in range(20)] + [210.0, 220.0, 230.0, 240.0]         # 105 … 200 每 $5
r = candidate(chain_on_grid(grid, 104.7), 104.7)
check("close≈105、±20% 带内 12 档全双边、点差 1.5% → 准入", r.get("verdict") is True,
      f"n_two_sided={r['metrics']['n_two_sided_strikes']} checks={r['checks']}")
r = candidate(chain_on_grid([7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0], 10.4,
                            spread=0.04, one_sided=(7.0, 13.0)), 10.4)
check("带内 4 档双边（NOK 档）→ 挡", r.get("verdict") is False,
      f"n_two_sided={r['metrics']['n_two_sided_strikes']}")
r = candidate(chain_on_grid([5.0, 7.5, 10.0], 7.4, one_sided=(5.0, 7.5, 10.0)), 7.4)
check("ATM 无双边报价（FLNC 档）→ 挡", r.get("verdict") is False and not r["checks"]["atm_spread_ok"],
      f"checks={r['checks']}")

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

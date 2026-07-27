"""准入阈值回归（2026-07-27 点差 3%→10% 校准）。

纯合成 chain，不实拉网络：只验 _metrics_from_chain 的度量 + 阈值常量语义。
    .venv/bin/python tests/test_admission.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from rnd import admission

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


print("[常量] 07-27 校准值")
check("MAX_ATM_SPREAD = 0.10", admission.MAX_ATM_SPREAD == 0.10, str(admission.MAX_ATM_SPREAD))
check("MIN_TWO_SIDED = 15", admission.MIN_TWO_SIDED == 15, str(admission.MIN_TWO_SIDED))

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
check("双边 6 个被 coverage 挡（<15）", not v, f"n_two_sided={m['n_two_sided_strikes']}")

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

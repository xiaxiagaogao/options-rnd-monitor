"""合成数据验证：平坦 IV ⇒ RND 应还原对数正态，分位数对上解析解。

用法：.venv/bin/python tests/test_synthetic.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from scipy.stats import norm

from rnd.pipeline.bl import extract_rnd
from rnd.pipeline.checks import no_arbitrage_checks
from rnd.pipeline.clean import filter_for_fit, otm_splice
from rnd.pipeline.forward import implied_forward
from rnd.pipeline.iv import black76_price, implied_vol
from rnd.pipeline.smooth import SmoothedSmile

F_TRUE, T, R, SIGMA = 100.0, 39 / 365, 0.04, 0.20
HALF_SPREAD = 0.02

# --- 造链：70–130 全行权价、双边报价 ---
rows = []
for k in np.arange(70, 130.5, 1.0):
    for right in ("C", "P"):
        mid = black76_price(F_TRUE, k, SIGMA, T, R, right)
        rows.append({"strike": k, "right": right,
                     "bid": max(mid - HALF_SPREAD, 0.0), "ask": mid + HALF_SPREAD})
chain = pd.DataFrame(rows)

# --- 全管线 ---
fitted = filter_for_fit(chain)
S_close = F_TRUE * np.exp(-R * T)  # 无股息时 S = F e^{-rT}
F, n_pairs = implied_forward(fitted, S_close, R, T)
otm = otm_splice(fitted, F)
otm["iv"] = [implied_vol(m, F, k, T, R, r_)
             for m, k, r_ in zip(otm["mid"], otm["strike"], otm["right"])]
otm = otm.dropna(subset=["iv"])
smile = SmoothedSmile(np.log(otm["strike"].values / F), otm["iv"].values,
                      otm["weight"].values)
rnd = extract_rnd(smile, F, T, R)
checks = no_arbitrage_checks(rnd)
mom = rnd.moments()

# --- 解析对照 ---
sT = SIGMA * np.sqrt(T)
analytic_q = {q: F_TRUE * np.exp(-0.5 * sT**2 + sT * norm.ppf(q))
              for q in (0.05, 0.25, 0.50, 0.75, 0.95)}
analytic_std = F_TRUE * np.sqrt(np.exp(sT**2) - 1)

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


print("=== 合成数据（平坦 IV 20%，对数正态）===")
check("F 反推", abs(F - F_TRUE) < 0.05, f"F={F:.4f} vs {F_TRUE}")
check("无套利全过", checks["all_pass"], str({k: v for k, v in checks.items() if k != 'all_pass'}))
check("IV 还原", abs(smile(0.0) - SIGMA) < 1e-3, f"ATM IV={smile(0.0):.5f} vs {SIGMA}")
check("std", abs(mom["std"] - analytic_std) / analytic_std < 0.02,
      f"{mom['std']:.4f} vs {analytic_std:.4f}")
for q, ref in analytic_q.items():
    kq, in_range = rnd.quantile(q)
    check(f"Q{int(q*100):02d}", abs(kq - ref) / ref < 0.003,
          f"{kq:.3f} vs {ref:.3f} (in_range={in_range})")
check("log_skew≈0（对数正态在对数空间无偏）", abs(mom["log_skew"]) < 0.02,
      f"log_skew={mom['log_skew']:.5f}")
mode, peaks = rnd.mode_and_peaks()
analytic_mode = F_TRUE * np.exp(-1.5 * sT**2)
check("众数", abs(mode - analytic_mode) / analytic_mode < 0.005,
      f"{mode:.3f} vs {analytic_mode:.3f}")
check("单峰", len(peaks) == 1, f"n_peaks={len(peaks)}")

if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

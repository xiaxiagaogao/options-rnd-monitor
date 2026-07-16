"""状态层滚动分位的单元验证（已知答案的合成序列）。

用法：.venv/bin/python tests/test_state.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from rnd.state import _delta_pct, rolling_percentile

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# --- 1. 样本门槛：前 59 天无分位，第 60 天起有 ---
n = 300
vals = np.arange(n).astype(float)
valid = np.ones(n, dtype=bool)
pct, sample_n = rolling_percentile(vals, valid, window=252, min_sample=60)
check("前 59 天分位为 NaN", np.all(np.isnan(pct[:59])), f"non-nan={np.sum(~np.isnan(pct[:59]))}")
check("第 60 天起有分位", not np.isnan(pct[59]), f"pct[59]={pct[59]:.2f}")
check("样本数递增到 window 封顶",
      sample_n[59] == 60 and sample_n[-1] == 252, f"sn[59]={sample_n[59]} sn[-1]={sample_n[-1]}")

# --- 2. 分位取值域与方向 ---
finite = pct[~np.isnan(pct)]
check("分位落在 [0,100]", finite.min() >= 0 and finite.max() <= 100,
      f"[{finite.min():.2f},{finite.max():.2f}]")
check("单调升序列当前值居分位顶", pct[100] > 99, f"pct[100]={pct[100]:.2f}")
pct_dec, _ = rolling_percentile(vals[::-1].copy(), valid, min_sample=60)
check("单调降序列当前值居分位底", pct_dec[100] < 1, f"pct_dec[100]={pct_dec[100]:.2f}")

# --- 3. 中位定标：常数噪声里插入已知分位 ---
rng = np.random.default_rng(0)
base = rng.normal(size=n)
base_sorted_val = np.median(base[:100])  # 一个已知≈50 分位的值
v3 = base.copy()
v3[100] = np.median(v3[:101])            # 令第 100 天恰为其窗口中位
pct3, _ = rolling_percentile(v3, valid, min_sample=60)
check("窗口中位 → 分位≈50", abs(pct3[100] - 50) < 5, f"pct[100]={pct3[100]:.2f}")

# --- 4. 闸门排除：失效日不进样本、不得分 ---
valid4 = np.ones(n, dtype=bool)
valid4[150:170] = False                  # 20 个闸门失败日
pct4, sn4 = rolling_percentile(vals, valid4, window=252, min_sample=60)
check("失效日分位为 NaN", np.all(np.isnan(pct4[150:170])), f"non-nan={np.sum(~np.isnan(pct4[150:170]))}")
# 第 200 天的窗口 [0..200] 内含 20 个失效日 → 样本 = 181
check("样本数扣除失效日", sn4[200] == 201 - 20, f"sn[200]={sn4[200]}")

# --- 5. roll 日 Δ 掩码 ---
roll = np.zeros(n)
roll[[80, 160, 240]] = 1
dpct = _delta_pct(pct, roll)
check("roll 日 Δ 为 NaN", np.all(np.isnan(dpct[[80, 160, 240]])), "")
check("非 roll 日 Δ 正常", not np.isnan(dpct[100]), f"dpct[100]={dpct[100]:.3f}")

if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

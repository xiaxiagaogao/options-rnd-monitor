"""止盈止损曲线纯计算（exit-curves spec §7）。合成数据，无网络；
QQQ 回归段仅在本地库有 2026-09-24 那一行时运行，否则 SKIP。

    .venv/bin/python tests/test_exit_curves.py
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from scipy.stats import norm

from rnd import exit_curves as ec
from rnd.config import DB_PATH

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


# 零漂移对数正态（S 是鞅，均值 = S0）；网格仿管线用对数等距，±8 个标准差免截断误差
S0, SIG, T = 100.0, 0.45, 30 / 365
V = SIG * np.sqrt(T)
GRID = S0 * np.exp(np.linspace(-8 * V, 8 * V, 4001))
CDF = norm.cdf((np.log(GRID / S0) + 0.5 * V**2) / V)
UP_K = S0 * np.array([1.02, 1.05, 1.10, 1.20, 1.35])
DN_K = S0 * np.array([0.98, 0.95, 0.90, 0.80, 0.65])


def bs_touch(K):
    """零漂移 GBM 的连续监控首达概率（反射原理闭式解）。"""
    b = np.log(K / S0)
    if K >= S0:
        return norm.cdf((-b - V**2 / 2) / V) + (S0 / K) * norm.cdf((-b + V**2 / 2) / V)
    return norm.cdf((b + V**2 / 2) / V) + (S0 / K) * norm.cdf((b - V**2 / 2) / V)


def side_vals(K, side, strikes=GRID, cdf=CDF):
    return (ec.expiry_prob(K, strikes, cdf, side), ec.touch_upper(K, strikes, cdf, side))


def test_bounds():
    print("\n[1] 上沿 ≥ 下沿，且上沿 ≤ 1")
    for side, K in (("up", np.linspace(S0, GRID[-1] * 1.1, 400)),
                    ("down", np.linspace(GRID[0] * 0.9, S0, 400))):
        lo, hi = side_vals(K, side)
        check(f"{side}: hi ≥ lo", bool(np.all(hi >= lo)))
        check(f"{side}: hi ≤ 1 且 lo ≥ 0", bool(np.all(hi <= 1) and np.all(lo >= 0)))
        check(f"{side}: 全部有限", bool(np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))))


def test_monotone():
    print("\n[2] 离 S0 越远，两者都单调下降")
    K = np.linspace(S0, S0 * 1.6, 300)            # 往上越走越远
    lo, hi = side_vals(K, "up")
    check("up: 到期概率单调降", bool(np.all(np.diff(lo) <= 1e-12)))
    check("up: 触及上沿单调降", bool(np.all(np.diff(hi) <= 1e-12)))
    K = np.linspace(S0, S0 * 0.5, 300)            # 往下越走越远
    lo, hi = side_vals(K, "down")
    check("down: 到期概率单调降", bool(np.all(np.diff(lo) <= 1e-12)))
    check("down: 触及上沿单调降", bool(np.all(np.diff(hi) <= 1e-12)))


def test_bs_inside():
    print("\n[3] BS 首达概率落在 [下沿, 上沿] 内（与终值分布相容的一个模型）")
    for side, Ks in (("up", UP_K), ("down", DN_K)):
        lo, hi = side_vals(Ks, side)
        for K, l, h in zip(Ks, lo, hi):
            bs = bs_touch(K)
            check(f"{side} K={K:.0f}: {l:.4f} ≤ BS {bs:.4f} ≤ {h:.4f}",
                  l <= bs + 1e-9 and bs <= h + 1e-6)
        # 区间非平凡：上沿明显低于 1，否则第 3 条测不出东西
        check(f"{side}: 最远一档上沿 < 0.5", hi[-1] < 0.5, f"{hi[-1]:.4f}")


def test_symmetry():
    print("\n[4] 上方与下方对称：镜像分布（绕 S0 翻转）上 K ↔ 2·S0−K 结果一致")
    m_strikes = 2 * S0 - GRID[::-1]
    m_cdf = 1.0 - CDF[::-1]
    lo_u, hi_u = side_vals(UP_K, "up")
    lo_d, hi_d = side_vals(2 * S0 - UP_K, "down", m_strikes, m_cdf)
    check("原分布上方 = 镜像下方（到期）", bool(np.allclose(lo_u, lo_d, atol=1e-9)))
    check("原分布上方 = 镜像下方（上沿）", bool(np.allclose(hi_u, hi_d, atol=1e-9)))
    lo_d2, hi_d2 = side_vals(DN_K, "down")
    lo_u2, hi_u2 = side_vals(2 * S0 - DN_K, "up", m_strikes, m_cdf)
    check("原分布下方 = 镜像上方（到期）", bool(np.allclose(lo_d2, lo_u2, atol=1e-9)))
    check("原分布下方 = 镜像上方（上沿）", bool(np.allclose(hi_d2, hi_u2, atol=1e-9)))


def test_pnl_sign():
    print("\n[5] 盈亏符号：多头往上赚、空头往下赚")
    K = np.array([90.0, 110.0])
    long_ = ec.locked_amount(K, anchor=100.0, qty=2.0)
    short = ec.locked_amount(K, anchor=100.0, qty=-2.0)
    check("多头：90 亏 20，110 赚 20", np.allclose(long_, [-20.0, 20.0]), f"{long_}")
    check("空头：90 赚 20，110 亏 20", np.allclose(short, [20.0, -20.0]), f"{short}")
    br = ec.branches(GRID, CDF, S0, np.linspace(70, 130, 61), anchor=100.0, qty=-3.0)
    check("branches 空头：下方分支（K<锚点）锁定为正", bool(np.all(br["down"]["locked"][:-1] > 0)))
    check("branches 空头：上方分支（K>锚点）锁定为负", bool(np.all(br["up"]["locked"][1:] < 0)))
    br0 = ec.branches(GRID, CDF, S0, np.linspace(70, 130, 61), anchor=100.0, qty=None)
    check("无持仓：locked 为 None", br0["down"]["locked"] is None and br0["up"]["locked"] is None)


def test_branches_and_axis():
    print("\n[附] 分支在 S0 交汇、网格外为 0、价位轴包含锚点")
    br = ec.branches(GRID, CDF, S0, np.linspace(60, 140, 81), anchor=100.0, qty=1.0)
    check("down 分支以 S0 结尾", br["down"]["prices"][-1] == S0)
    check("up 分支以 S0 开头", br["up"]["prices"][0] == S0)
    check("S0 处触及上沿 ≈ 1", br["up"]["touch_hi"][0] > 0.999 and br["down"]["touch_hi"][-1] > 0.999)
    lo, hi = side_vals(np.array([GRID[-1] * 1.05]), "up")
    check("网格外（上方）两者为 0", lo[0] == 0 and hi[0] == 0, f"{lo[0]}, {hi[0]}")
    noisy = CDF.copy()
    noisy[2000] = noisy[1999] - 1e-4            # 注入一个非单调点
    check("CDF 清理后非降", bool(np.all(np.diff(ec.clean_cdf(noisy)) >= 0)))
    a, b = ec.axis_range([(GRID, CDF)], [100.0, 180.0])
    check("价位轴覆盖 Q02–Q98 与锚点", a < ec.quantile(GRID, CDF, 0.02) and b > 180.0, f"[{a:.1f}, {b:.1f}]")


def test_qqq_regression():
    print("\n[回归] QQQ 2026-09-24（spec §7 验收数字）")
    p = Path(DB_PATH)
    r = None
    if p.exists():
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            r = c.execute(
                "SELECT i.forward, i.sigma1_pct, cu.grid_json FROM rnd_indicators i"
                " JOIN rnd_curve cu USING (date, symbol, expiry)"
                " WHERE i.symbol='QQQ' AND i.date='2026-09-24' AND i.pinned=1").fetchone()
        except sqlite3.Error:
            r = None
        finally:
            c.close()
    if r is None:
        print("  SKIP  本地库无 QQQ 2026-09-24 pinned 行")
        return
    F, sp, gj = r
    g = json.loads(gj)
    s, cdf = np.asarray(g["strikes"]), np.asarray(g["cdf"])
    expect = {0.5: (0.306, 0.686), 1.0: (0.120, 0.299), 1.5: (0.034, 0.091), 2.0: (0.008, 0.024)}
    for m, (lo_e, hi_e) in expect.items():
        K = F * (1 + m * sp)
        lo = ec.expiry_prob([K], s, cdf, "up")[0]
        hi = ec.touch_upper([K], s, cdf, "up")[0]
        check(f"+{m}σ K={K:.2f}: 到期 {lo:.3%} / 上沿 {hi:.3%}",
              abs(lo - lo_e) < 6e-4 and abs(hi - hi_e) < 6e-4)
    K = F * (1 + 0.5 * sp)
    C, _ = ec.option_integrals(s, cdf)
    mask = s < K
    j = int(np.argmin(C[mask] / (K - s[mask])))
    y = s[mask][j]
    check(f"+0.5σ 最小值位置 y={y:.2f} C={C[mask][j]:.2f}",
          abs(y - 730.39) < 0.05 and abs(C[mask][j] - 20.08) < 0.01)


if __name__ == "__main__":
    for t in (test_bounds, test_monotone, test_bs_inside, test_symmetry, test_pnl_sign,
              test_branches_and_axis, test_qqq_regression):
        t()
    print(f"\n{'ALL PASS' if not _failed else 'FAILED: ' + ', '.join(_failed)}")
    sys.exit(1 if _failed else 0)

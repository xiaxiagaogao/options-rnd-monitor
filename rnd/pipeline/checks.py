"""无套利与质量检查（spec §5.7）——即验收标准，闸门非信号。"""
import numpy as np

from ..config import PIPELINE
from .bl import RNDResult


def no_arbitrage_checks(rnd: RNDResult, region: str = "all") -> dict:
    """region='quoted' 只检查报价区内（外推区问题由 fit 层的翼部斜率封顶单独治理）。"""
    c, k, density = rnd.call_prices, rnd.strikes, rnd.density
    if region == "quoted":
        m = (k >= rnd.k_quoted[0]) & (k <= rnd.k_quoted[1])
        c, k, density = c[m], k[m], density[m]
    slopes = np.diff(c) / np.diff(k)   # 网格非均匀，须用斜率而非原始差分
    monotone = bool(np.all(slopes <= 1e-9))
    # 容差相对密度峰值：tick 量化噪声致万分位级负密度，属数值噪声非真套利
    tol = PIPELINE["density_neg_rel_tol"] * float(rnd.density.max())
    d2 = np.diff(slopes) / ((k[2:] - k[:-2]) / 2)   # ≈ C''(K) = D·density
    convex = bool(np.all(d2 >= -tol * np.exp(-rnd.r * rnd.T)))
    min_density = float(density.min())
    density_nonneg = bool(min_density >= -tol)
    mass = float(np.trapezoid(rnd.density, rnd.strikes))   # 质量恒用全网格
    integral_ok = bool(abs(mass - 1.0) <= PIPELINE["integral_tol"])
    return {
        "monotone_decreasing": monotone,
        "convex": convex,
        "density_nonneg": density_nonneg,
        "min_density": min_density,
        "integral_mass": mass,
        "integral_ok": integral_ok,
        "all_pass": monotone and convex and density_nonneg and integral_ok,
    }


def rr_skew_gate(rr25: float, log_skew: float) -> bool:
    """RR 与偏度同向性（校验类，spec §4）。

    比较口径必须是对数空间偏度：价格空间矩偏度自带对数正态正基线，
    高波动个股（如 NVDA）会被基线淹没导致系统性误杀（2026-07-14 实测 519 例）。
    均视零为中性，容忍小幅错位。"""
    if rr25 is None or log_skew is None or np.isnan(rr25) or np.isnan(log_skew):
        return False
    return bool(np.sign(rr25) == np.sign(log_skew)
                or abs(rr25) < 0.002 or abs(log_skew) < 0.05)

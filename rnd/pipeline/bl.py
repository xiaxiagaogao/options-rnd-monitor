"""Breeden-Litzenberger（spec §5.5–5.6）：平滑 IV → C(K) 细网格 → 密度 / CDF / 分位数。"""
import numpy as np
from scipy.stats import norm

from ..config import PIPELINE
from .smooth import SmoothedSmile


class RNDResult:
    def __init__(self, strikes, call_prices, density, cdf, F, T, r, smile: SmoothedSmile):
        self.strikes, self.call_prices = strikes, call_prices
        self.density, self.cdf = density, cdf
        self.F, self.T, self.r, self.smile = F, T, r, smile
        self.k_quoted = (F * np.exp(smile.x_min), F * np.exp(smile.x_max))

    def quantile(self, q: float) -> tuple[float, bool]:
        """CDF 反查（一阶导路径，比密度稳一个量级）。返回 (K, in_quoted_range)。"""
        k = float(np.interp(q, self.cdf, self.strikes))
        return k, bool(self.k_quoted[0] <= k <= self.k_quoted[1])

    def moments(self) -> dict:
        f, k = self.density, self.strikes
        m = np.trapezoid(f, k)                       # 概率质量（含尾部截断损失）
        mean = np.trapezoid(k * f, k) / m
        var = np.trapezoid((k - mean) ** 2 * f, k) / m
        std = np.sqrt(var)
        skew = np.trapezoid((k - mean) ** 3 * f, k) / m / std**3
        kurt = np.trapezoid((k - mean) ** 4 * f, k) / m / std**4
        # 对数空间偏度：对数正态基线恰为 0，符号纯由 smile 斜率驱动，
        # 与 RR25 可比（价格空间偏度自带 +基线，高波动个股会被基线淹没）
        lk = np.log(k)
        lmean = np.trapezoid(lk * f, k) / m
        lstd = np.sqrt(np.trapezoid((lk - lmean) ** 2 * f, k) / m)
        lskew = np.trapezoid((lk - lmean) ** 3 * f, k) / m / lstd**3
        return {"mass": float(m), "mean": float(mean), "std": float(std),
                "skew": float(skew), "ex_kurt": float(kurt - 3.0),
                "log_skew": float(lskew)}

    def mode_and_peaks(self, min_prominence_frac=0.05) -> tuple[float, list]:
        """密度峰值位 + 局部极大值清单，各峰带概率质量（谷底分割积分，spec §4 事件类）。"""
        f, k = self.density, self.strikes
        idx = [i for i in range(1, len(f) - 1)
               if f[i] > f[i - 1] and f[i] > f[i + 1] and f[i] > min_prominence_frac * f.max()]
        # 相邻峰之间的谷底为质量分割边界
        bounds = [0]
        for a, b in zip(idx, idx[1:]):
            bounds.append(a + int(np.argmin(f[a:b + 1])))
        bounds.append(len(f) - 1)
        total = np.trapezoid(f, k)
        peaks = []
        for j, i in enumerate(idx):
            lo, hi = bounds[j], bounds[j + 1]
            mass = np.trapezoid(f[lo:hi + 1], k[lo:hi + 1]) / total
            peaks.append({"strike": float(k[i]), "density": float(f[i]),
                          "mass": float(mass)})
        mode = float(k[np.argmax(f)])
        return mode, peaks

    def tail_prob(self, pct: float) -> tuple[float, float]:
        """P(S < F*(1-pct)), P(S > F*(1+pct))，从 CDF 取。"""
        lo = float(np.interp(self.F * (1 - pct), self.strikes, self.cdf))
        hi = 1.0 - float(np.interp(self.F * (1 + pct), self.strikes, self.cdf))
        return lo, hi


def extract_rnd(smile: SmoothedSmile, F: float, T: float, r: float) -> RNDResult:
    x_lo = smile.x_min - PIPELINE["grid_pad_x"]
    x_hi = smile.x_max + PIPELINE["grid_pad_x"]
    strikes = F * np.exp(np.linspace(x_lo, x_hi, PIPELINE["grid_points"]))
    iv = smile(np.log(strikes / F))
    # 向量化 Black-76 call（iv 有 0.01 下限，st > 0 恒成立）
    st = iv * np.sqrt(T)
    d1 = (np.log(F / strikes) + 0.5 * st**2) / st
    d2 = d1 - st
    call = np.exp(-r * T) * (F * norm.cdf(d1) - strikes * norm.cdf(d2))

    D = np.exp(-r * T)
    dc_dk = np.gradient(call, strikes)
    d2c_dk2 = np.gradient(dc_dk, strikes)
    density = d2c_dk2 / D          # f(K) = e^{rT} C''(K)
    cdf = 1.0 + dc_dk / D          # F(K) = 1 + e^{rT} C'(K)
    cdf = np.clip(cdf, 0.0, 1.0)
    return RNDResult(strikes, call, density, cdf, F, T, r, smile)

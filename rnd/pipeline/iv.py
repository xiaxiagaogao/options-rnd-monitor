"""Black-76（对 F 计价）定价与 IV 反解（spec §5.3）。"""
import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm


def black76_price(F, K, sigma, T, r, right="C"):
    if sigma <= 0 or T <= 0:
        intrinsic = max(F - K, 0.0) if right == "C" else max(K - F, 0.0)
        return np.exp(-r * T) * intrinsic
    st = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * st**2) / st
    d2 = d1 - st
    D = np.exp(-r * T)
    if right == "C":
        return D * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return D * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def implied_vol(price, F, K, T, r, right="C") -> float:
    """BS 反解。无解（价格越出无套利界）返回 nan。"""
    D = np.exp(-r * T)
    intrinsic = D * max(F - K, 0.0) if right == "C" else D * max(K - F, 0.0)
    upper = D * F if right == "C" else D * K
    if not (intrinsic < price < upper):
        return np.nan
    try:
        return brentq(
            lambda s: black76_price(F, K, s, T, r, right) - price,
            1e-4, 5.0, xtol=1e-8,
        )
    except ValueError:
        return np.nan


def forward_delta_strike(F, sigma, T, delta: float, right="C") -> float:
    """给定 forward delta 反解行权价（25Δ RR 用）。call: N(d1)=Δ；put: N(d1)=1-|Δ|。"""
    st = sigma * np.sqrt(T)
    nd1 = delta if right == "C" else 1.0 - abs(delta)
    d1 = norm.ppf(nd1)
    return float(F * np.exp(0.5 * st**2 - d1 * st))

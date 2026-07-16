"""IV 空间平滑（spec §5.4）：cubic smoothing spline on (log-moneyness, IV)。

禁止在价格空间直接平滑（坑清单第 1 条）。平滑参数入 fit_meta。
"""
import numpy as np
from scipy.interpolate import UnivariateSpline

from ..config import PIPELINE


class SmoothedSmile:
    """平滑后的 IV 曲线。报价区外 flat 外推（显式，边界入 fit_meta）。"""

    def __init__(self, x: np.ndarray, iv: np.ndarray, weights: np.ndarray,
                 s_multiplier: float = 1.0, wing_scale: float = 1.0):
        order = np.argsort(x)
        self.x, self.iv, w = x[order], iv[order], weights[order]
        self.x_min, self.x_max = float(self.x[0]), float(self.x[-1])
        # s 按预期残差定标：s = N * resid²（权重归一后残差以 IV 为单位）。
        # s_multiplier 由自适应循环控制：取通过无套利检查的最小充分平滑。
        w = w / w.mean()
        self.s_multiplier = s_multiplier
        self.s = len(self.x) * PIPELINE["spline_resid_iv"] ** 2 * s_multiplier
        self.spline = UnivariateSpline(
            self.x, self.iv, w=w, k=PIPELINE["spline_k"], s=self.s
        )
        resid = self.iv - self.spline(self.x)
        # 加权 RMSE：翼部低权重点不主导拟合误差口径
        self.rmse = float(np.sqrt(np.sum(w * resid**2) / np.sum(w)))
        # 外推参数：总方差 w=σ²T 对 x 线性延伸（业界标准 wing，渐进等价对数正态尾；
        # 斜率衰减/flat 钳制两种方案实测都会在陡翼接缝制造蝶式套利，已弃用）。
        # d(σ²)/dx = 2·σ_b·σ'_b，与 T 无关。
        # wing_scale ∈ [0,1]：外推斜率封顶系数——短 DTE 极陡翼的边界斜率线性延伸会
        # 违反正密度条件（Gatheral g<0），由 fit 层逐级降标搜索，取最大可行值。
        self.wing_scale = wing_scale
        d = self.spline.derivative()
        self._b = {
            "lo": (float(self.spline(self.x_min)), float(d(self.x_min)) * wing_scale),
            "hi": (float(self.spline(self.x_max)), float(d(self.x_max)) * wing_scale),
        }

    def __call__(self, x):
        """报价区内走样条；区外总方差线性外推（C1 连续，显式声明入 fit_meta）。"""
        x_in = np.asarray(x, dtype=float)
        scalar = x_in.ndim == 0
        x = np.atleast_1d(x_in)
        out = np.asarray(self.spline(np.clip(x, self.x_min, self.x_max)))
        lo_v, lo_d = self._b["lo"]
        hi_v, hi_d = self._b["hi"]
        below, above = x < self.x_min, x > self.x_max
        if below.any():
            var = lo_v**2 + 2 * lo_v * lo_d * (x[below] - self.x_min)
            out[below] = np.sqrt(np.maximum(var, 1e-4))
        if above.any():
            var = hi_v**2 + 2 * hi_v * hi_d * (x[above] - self.x_max)
            out[above] = np.sqrt(np.maximum(var, 1e-4))
        out = np.maximum(out, 0.01)
        return float(out[0]) if scalar else out

    @property
    def fit_meta(self) -> dict:
        return {
            "method": "cubic_smoothing_spline",
            "smoothing_s": self.s,
            "s_multiplier": self.s_multiplier,
            "resid_iv_target": PIPELINE["spline_resid_iv"],
            "n_strikes_used": int(len(self.x)),
            "fit_rmse_iv": self.rmse,
            "extrapolation": PIPELINE["extrapolation"],
            "wing_scale": self.wing_scale,
            "wing_slopes": {"lo": self._b["lo"][1], "hi": self._b["hi"][1]},
            "x_quoted_range": [self.x_min, self.x_max],
        }

"""自适应拟合：s 从最小值倍增，直到报价区内无套利（最小充分平滑，保双峰）。

坑清单的两难：平滑不足 → density 负值；平滑过度 → 双峰被抹平。

分区治理：闸门口径 = 报价区（全部信号指标的承载区）；外推区（翼部斜率过陡时
线性总方差延伸可违反 Gatheral g≥0，为数学事实非参数问题）违规如实记录进
fit_meta，Q05/Q95 落入时由 in_quoted_range=False 打标兜底。
V1 升级路径：对数正态尾部嫁接（Figlewski 式）。
"""
import numpy as np

from .bl import RNDResult, extract_rnd
from .checks import no_arbitrage_checks
from .smooth import SmoothedSmile

MAX_DOUBLINGS = 10


def fit_rnd_no_arb(x: np.ndarray, iv: np.ndarray, weights: np.ndarray,
                   F: float, T: float, r: float) -> tuple[RNDResult, dict]:
    """返回 (rnd, checks)。checks 为报价区口径，另附 full_grid 全网格结果。"""
    mult = 1.0
    for _ in range(MAX_DOUBLINGS + 1):
        smile = SmoothedSmile(x, iv, weights, s_multiplier=mult)
        rnd = extract_rnd(smile, F, T, r)
        checks = no_arbitrage_checks(rnd, region="quoted")
        if checks["all_pass"]:
            break
        mult *= 2
    checks["full_grid"] = no_arbitrage_checks(rnd)
    return rnd, checks

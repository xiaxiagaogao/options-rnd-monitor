"""隐含远期 F（spec §5.2）：同到期 put-call parity，ATM 邻域中值。"""
import numpy as np
import pandas as pd

from ..config import PIPELINE


def implied_forward(fitted: pd.DataFrame, underlying_close: float,
                    r: float, T: float) -> tuple[float, int]:
    """C - P = e^{-rT}(F - K)  =>  F = K + e^{rT}(C - P)

    fitted：filter_for_fit 之后的链（含 mid）。
    返回 (F, 使用的行权价对数)。
    """
    calls = fitted[fitted["right"] == "C"].set_index("strike")["mid"]
    puts = fitted[fitted["right"] == "P"].set_index("strike")["mid"]
    common = calls.index.intersection(puts.index)
    if len(common) == 0:
        raise ValueError("无双边报价的 call/put 行权价对，无法反推 F")
    # ATM 邻域：距标的收盘最近的 N 个对
    strikes = pd.Series(common, index=common)
    nearest = strikes.sub(underlying_close).abs().nsmallest(PIPELINE["parity_pairs"]).index
    f_values = [k + np.exp(r * T) * (calls[k] - puts[k]) for k in nearest]
    return float(np.median(f_values)), len(nearest)

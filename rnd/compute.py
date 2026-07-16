"""单日单到期的完整管线计算：raw chain → 曲线 + 指标行。

skeleton 和 backfill 共用。失败抛 ComputeError（调用方记录并跳过）。
"""
import datetime as dt
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import PIPELINE
from .pipeline.bl import RNDResult
from .pipeline.checks import rr_skew_gate
from .pipeline.clean import filter_for_fit, otm_splice
from .pipeline.fit import fit_rnd_no_arb
from .pipeline.forward import implied_forward
from .pipeline.iv import forward_delta_strike, implied_vol


class ComputeError(RuntimeError):
    pass


@dataclass
class DayResult:
    rnd: RNDResult
    checks: dict
    fit_meta: dict
    grid: dict
    indicators: dict


def compute_day(chain: pd.DataFrame, underlying_close: float, sofr: float,
                asof: dt.date, expiry: dt.date, symbol: str,
                tail_pct: float = 0.05) -> DayResult:
    """chain 列要求：strike, right('C'/'P'), bid, ask。tail_pct：指数 5%，个股 10%。"""
    dte = (expiry - asof).days
    T = dte / PIPELINE["day_count"]

    fitted = filter_for_fit(chain)
    if len(fitted) < 20:
        raise ComputeError(f"清洗后仅 {len(fitted)} 行")
    try:
        F, n_pairs = implied_forward(fitted, underlying_close, sofr, T)
    except ValueError as e:
        raise ComputeError(str(e)) from e

    otm = otm_splice(fitted, F)
    otm["iv"] = [implied_vol(m, F, k, T, sofr, r_)
                 for m, k, r_ in zip(otm["mid"], otm["strike"], otm["right"])]
    otm = otm.dropna(subset=["iv"]).reset_index(drop=True)
    if len(otm) < 10:
        raise ComputeError(f"IV 反解成功仅 {len(otm)} 个行权价")

    rnd, checks = fit_rnd_no_arb(np.log(otm["strike"].values / F), otm["iv"].values,
                                 otm["weight"].values, F, T, sofr)
    smile = rnd.smile
    mom = rnd.moments()
    mode, peaks = rnd.mode_and_peaks()

    quantiles = {q: rnd.quantile(q) for q in (0.05, 0.25, 0.50, 0.75, 0.95)}
    q05, q25, q50, q75, q95 = (quantiles[q][0] for q in (0.05, 0.25, 0.50, 0.75, 0.95))
    atm_iv = float(smile(0.0))
    k25c = forward_delta_strike(F, atm_iv, T, 0.25, "C")
    k25p = forward_delta_strike(F, atm_iv, T, -0.25, "P")
    iv25c = float(smile(np.log(k25c / F)))
    iv25p = float(smile(np.log(k25p / F)))
    rr25 = iv25c - iv25p
    bf25 = (iv25c + iv25p) / 2 - atm_iv
    bowley = ((q95 - q50) - (q50 - q05)) / (q95 - q05)
    tail_down, tail_up = rnd.tail_prob(tail_pct)

    gate_detail = {
        "no_arb": checks["all_pass"],
        "rr_skew_agree": rr_skew_gate(rr25, mom["log_skew"]),
        "n_strikes_ok": len(otm) >= 25,
        "fit_rmse_ok": smile.rmse < 0.01,
    }
    gate_pass = all(gate_detail.values())

    fit_meta = smile.fit_meta | {
        "checks": checks, "n_parity_pairs": n_pairs,
        "day_count": PIPELINE["day_count"], "grid_pad_x": PIPELINE["grid_pad_x"],
        "tail_pct": tail_pct,
    }
    indicators = {
        "date": str(asof), "symbol": symbol, "expiry": str(expiry), "dte": dte,
        "forward": F, "sigma1_abs": mom["std"], "sigma1_pct": mom["std"] / F,
        "q05": q05, "q25": q25, "q50": q50, "q75": q75, "q95": q95,
        **{f"q{int(q*100):02d}_in_range": int(v[1]) for q, v in quantiles.items()},
        "mode": mode, "skew": mom["skew"], "ex_kurt": mom["ex_kurt"],
        "log_skew": mom["log_skew"],
        "atm_iv": atm_iv, "rr25": rr25, "bowley_skew": bowley, "bf25": bf25,
        "term_slope": None,   # 需同日两个到期，upsert 后由 term/pin 后处理补
        "tail_p_down": tail_down, "tail_p_up": tail_up,
        "n_modes": len(peaks), "modes_json": json.dumps(peaks),
        "gate_pass": int(gate_pass), "gate_detail": json.dumps(gate_detail),
    }
    grid = {"strikes": rnd.strikes.tolist(), "density": rnd.density.tolist(),
            "cdf": rnd.cdf.tolist()}
    return DayResult(rnd, checks, fit_meta, grid, indicators)

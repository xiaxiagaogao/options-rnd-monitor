"""清洗（spec §5.1）：入库打标 + 计算时过滤，两件事分开。"""
import numpy as np
import pandas as pd

from ..config import PIPELINE


def quality_flags(bid: float, ask: float, wide_thresh: float = None) -> str:
    """入库打标（不剔除）：one_sided / crossed / wide_spread。"""
    wide_thresh = wide_thresh or PIPELINE["wide_spread_flag"]
    flags = []
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        flags.append("one_sided")
    elif bid > ask:
        flags.append("crossed")
    else:
        mid = (bid + ask) / 2
        if mid > 0 and (ask - bid) / mid > wide_thresh:
            flags.append("wide_spread")
    return ",".join(flags)


def filter_for_fit(chain: pd.DataFrame) -> pd.DataFrame:
    """计算时过滤：剔除 crossed / 单边 / 相对点差超阈值；加 mid 与权重列。

    chain 列要求：strike, right('C'/'P'), bid, ask。
    """
    df = chain.copy()
    df = df[(df["bid"] > 0) & (df["ask"] > 0)]          # 单边剔除
    df = df[df["bid"] <= df["ask"]]                       # crossed 剔除
    df["mid"] = (df["bid"] + df["ask"]) / 2
    df["rel_spread"] = (df["ask"] - df["bid"]) / df["mid"]
    df = df[df["rel_spread"] <= PIPELINE["rel_spread_max"]]
    # 降权：点差越宽权重越低；下限截断防 ATM 极窄点差垄断拟合
    df["weight"] = 1.0 / np.clip(df["rel_spread"], 0.02, None)
    return df.reset_index(drop=True)


def otm_splice(fitted: pd.DataFrame, forward: float) -> pd.DataFrame:
    """仅用 OTM：K >= F 取 call，K < F 取 put（缓解美式偏差），一律用 mid。"""
    calls = fitted[(fitted["right"] == "C") & (fitted["strike"] >= forward)]
    puts = fitted[(fitted["right"] == "P") & (fitted["strike"] < forward)]
    return (
        pd.concat([puts, calls])
        .sort_values("strike")
        .drop_duplicates(subset="strike")
        .reset_index(drop=True)
    )

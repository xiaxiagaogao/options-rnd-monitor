"""252 日滚动分位状态层（spec §4 通则）。

把状态类指标的水平值变换为可交易信号：
- 252 日滚动分位（trailing window；仅闸门通过且非空日入参考样本）
- 日环比 Δ 分位（roll 日置 NULL，避免换月锯齿假信号）
- 有效样本 < 60 不出分位（防假精确）

在 pinned 序列（最接近 30 DTE 那套）上计算，纯派生可全量重算，输出 rnd_state。
"""
import numpy as np
import pandas as pd
from scipy.stats import percentileofscore

from . import db

# 状态类指标（spec §4）。定位类（分位数/F/众数）为水平量，不入此层。
STATE_INDICATORS = [
    "atm_iv",       # 等价 IV rank：高分位防 vol crush，低分位适合埋伏
    "rr25",         # 25Δ 风险反转，情绪计
    "skew",         # 价格空间偏度（含对数正态基线，衡量分布本身不对称）
    "log_skew",     # 对数空间偏度（基线归零，纯 smile 驱动）
    "bowley_skew",  # 分位数偏度，稳健口径
    "ex_kurt",      # 超额峰度
    "bf25",         # 25Δ 蝶式，峰度独立口径
    "term_slope",   # 期限结构斜率，事件压力计
    "tail_p_down",  # 下行尾部概率
    "tail_p_up",    # 上行尾部概率
]

WINDOW = 252
MIN_SAMPLE = 60


def rolling_percentile(values: np.ndarray, valid: np.ndarray,
                       window: int = WINDOW, min_sample: int = MIN_SAMPLE):
    """滚动分位（纯函数）。

    values: 原始水平值序列（可含 nan）。
    valid:  bool 序列，True = 该日可入参考样本且可被打分（闸门通过且值非空）。
    对每个 valid 日 i，在 trailing window 内的 valid 值集合中求 i 的分位（kind='mean'）；
    样本 < min_sample 或该日非 valid → nan。
    返回 (pct[0..100 或 nan], sample_n)。
    """
    n = len(values)
    pct = np.full(n, np.nan)
    sample_n = np.zeros(n, dtype=int)
    for i in range(n):
        lo = max(0, i - window + 1)
        w_valid = valid[lo:i + 1]
        sample = values[lo:i + 1][w_valid]
        sample_n[i] = len(sample)
        if valid[i] and sample_n[i] >= min_sample:
            pct[i] = percentileofscore(sample, values[i], kind="mean")
    return pct, sample_n


def _delta_pct(pct: np.ndarray, roll: np.ndarray) -> np.ndarray:
    """日环比分位 Δ。roll 日跨越到期不连续，置 nan（避免锯齿）。"""
    dpct = np.full(len(pct), np.nan)
    dpct[1:] = pct[1:] - pct[:-1]
    dpct[np.asarray(roll, dtype=bool)] = np.nan   # 换月日不算 Δ
    return dpct


def compute_state(conn, symbol: str) -> int:
    """读 pinned 序列，逐指标算分位 + Δ，写 rnd_state。返回写入行数。"""
    df = pd.read_sql_query(
        "SELECT date, gate_pass, roll, " + ", ".join(STATE_INDICATORS)
        + " FROM rnd_indicators WHERE symbol = ? AND pinned = 1 ORDER BY date",
        conn, params=(symbol,),
    )
    if df.empty:
        return 0
    gate = df["gate_pass"].fillna(0).to_numpy().astype(bool)
    roll = df["roll"].fillna(0).to_numpy()
    rows = []
    for ind in STATE_INDICATORS:
        vals = df[ind].to_numpy(dtype=float)
        valid = gate & ~np.isnan(vals)
        pct, sample_n = rolling_percentile(vals, valid)
        dpct = _delta_pct(pct, roll)
        for d, v, p, dp, sn in zip(df["date"], vals, pct, dpct, sample_n):
            rows.append((
                d, symbol, ind,
                None if np.isnan(v) else float(v),
                None if np.isnan(p) else float(p),
                None if np.isnan(dp) else float(dp),
                int(sn),
            ))
    db.replace_state(conn, symbol, rows)
    return len(rows)

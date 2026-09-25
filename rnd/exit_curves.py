"""止盈止损曲线的纯计算（docs/superpowers/specs/2026-09-25-exit-curves-design.md §3）。

输入某日 RND 的 CDF 网格与起点价 S0（该日 forward），给出每个价位 K 的：
  到期概率（实线 / 阴影带下沿）：K 在 S0 之上取 P(S_T ≥ K)，之下取 P(S_T ≤ K)
  触及上沿（阴影带上沿）：Brown–Hobson–Rogers (2001) 超复制界
    K > S0：min_{y<K} C(y)/(K−y)，C(y) = ∫_y^∞ (1−F) ds（不贴现看涨）
    K < S0：min_{y>K} P(y)/(y−K)，P(y) = ∫_{−∞}^y F ds（不贴现看跌）
  结果截在 [下沿, 1]。积分用网格梯形法，y 候选取网格点；网格外视为无概率质量。
无 IO，所有输入输出都是 numpy 数组。
"""
import numpy as np


def clean_cdf(cdf) -> np.ndarray:
    """截到 [0, 1] 再取累计最大值：修平数值导数带来的微小非单调。"""
    return np.maximum.accumulate(np.clip(np.asarray(cdf, dtype=float), 0.0, 1.0))


def option_integrals(strikes, cdf) -> tuple[np.ndarray, np.ndarray]:
    """网格点上的不贴现看涨 C(s_i) = ∫_{s_i}^{s_N} (1−F) 与看跌 P(s_i) = ∫_{s_1}^{s_i} F。"""
    s = np.asarray(strikes, dtype=float)
    F = clean_cdf(cdf)
    ds = np.diff(s)
    G = 1.0 - F
    seg_c = 0.5 * (G[1:] + G[:-1]) * ds
    seg_p = 0.5 * (F[1:] + F[:-1]) * ds
    C = np.concatenate([np.cumsum(seg_c[::-1])[::-1], [0.0]])
    P = np.concatenate([[0.0], np.cumsum(seg_p)])
    return C, P


def expiry_prob(prices, strikes, cdf, side: str) -> np.ndarray:
    """到期收在价位之外的概率。side='up' → 1−F(K)；'down' → F(K)。网格外按 0/1 外延。"""
    K = np.asarray(prices, dtype=float)
    Fk = np.interp(K, np.asarray(strikes, dtype=float), clean_cdf(cdf), left=0.0, right=1.0)
    return 1.0 - Fk if side == "up" else Fk


def touch_upper(prices, strikes, cdf, side: str) -> np.ndarray:
    """路上触及价位的概率上沿（无模型），已截到 [到期概率, 1]。"""
    s = np.asarray(strikes, dtype=float)
    K = np.asarray(prices, dtype=float)
    C, P = option_integrals(s, cdf)
    with np.errstate(divide="ignore", invalid="ignore"):
        if side == "up":
            gap = K[:, None] - s[None, :]                      # K − y，只取 y < K
            ratio = np.where(gap > 0, C[None, :] / gap, np.inf)
        else:
            gap = s[None, :] - K[:, None]                      # y − K，只取 y > K
            ratio = np.where(gap > 0, P[None, :] / gap, np.inf)
    bound = np.minimum(ratio.min(axis=1), 1.0)
    return np.clip(bound, expiry_prob(K, s, cdf, side), 1.0)


def locked_amount(prices, anchor: float, qty: float) -> np.ndarray:
    """按价位 K 全仓了结、相对锚点锁定的金额 qty × (K − anchor)；qty 带方向符号。"""
    return float(qty) * (np.asarray(prices, dtype=float) - float(anchor))


def quantile(strikes, cdf, q: float) -> float:
    return float(np.interp(q, clean_cdf(cdf), np.asarray(strikes, dtype=float)))


def axis_range(dists, anchors, lo_q: float = 0.02, hi_q: float = 0.98,
               pad: float = 0.04) -> tuple[float, float]:
    """价位轴：各分布 Q02–Q98 的并集 ∪ 锚点（成本、收盘），两端各留 pad×跨度。"""
    marks = [float(a) for a in anchors if a is not None]
    lo = min([quantile(s, c, lo_q) for s, c in dists] + marks)
    hi = max([quantile(s, c, hi_q) for s, c in dists] + marks)
    span = hi - lo
    return lo - pad * span, hi + pad * span


def branches(strikes, cdf, s0: float, prices, anchor: float, qty: float | None = None) -> dict:
    """把价位轴按 S0 拆成下方 / 上方两段（各自含 S0 端点，两段在 S0 交汇）。

    概率方向按 S0 判，盈亏按锚点判（spec §3）：两者对深套仓位会分开。"""
    K = np.asarray(prices, dtype=float)
    out = {}
    for side, pts in (("down", np.append(K[K < s0], s0)), ("up", np.insert(K[K > s0], 0, s0))):
        out[side] = {
            "prices": pts,
            "expiry_prob": expiry_prob(pts, strikes, cdf, side),
            "touch_hi": touch_upper(pts, strikes, cdf, side),
            "pct": pts / float(anchor) - 1.0,
            "locked": None if qty is None else locked_amount(pts, anchor, qty),
        }
    return out

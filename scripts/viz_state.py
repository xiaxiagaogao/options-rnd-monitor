"""状态层可视化：
1. 分位热力图（指标 × 时间，雷达面板的时序版）——SPY / NVDA
2. 最新交易日三标的状态对比（分位横向柱）——仪表盘头条快照
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from rnd import db
from rnd.config import OUTPUT_DIR
from rnd.state import STATE_INDICATORS

plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti TC", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False
conn = db.get_conn()
OUTPUT_DIR.mkdir(exist_ok=True)

LABELS = {
    "atm_iv": "ATM IV", "rr25": "25Δ RR", "skew": "偏度(价格)",
    "log_skew": "偏度(对数)", "bowley_skew": "偏度(Bowley)", "ex_kurt": "超额峰度",
    "bf25": "25Δ 蝶式", "term_slope": "期限斜率",
    "tail_p_down": "下尾概率", "tail_p_up": "上尾概率",
}


def heatmap(symbol, ax):
    df = pd.read_sql_query(
        "SELECT date, indicator, pct FROM rnd_state WHERE symbol=? ORDER BY date",
        conn, params=(symbol,))
    wide = df.pivot(index="indicator", columns="date", values="pct").reindex(STATE_INDICATORS)
    dates = pd.to_datetime(wide.columns)
    Z = wide.to_numpy(dtype=float)
    Z = np.ma.masked_invalid(Z)
    x = np.arange(Z.shape[1] + 1)          # flat shading 需边界比格子多 1
    cmap = plt.cm.RdYlGn_r.copy()
    cmap.set_bad("#dddddd")            # 未出分位 / 闸门失败 → 灰
    pm = ax.pcolormesh(x, np.arange(len(STATE_INDICATORS) + 1), Z,
                       cmap=cmap, vmin=0, vmax=100, shading="flat")
    ax.set_yticks(np.arange(len(STATE_INDICATORS)) + 0.5)
    ax.set_yticklabels([LABELS[i] for i in STATE_INDICATORS], fontsize=9)
    ax.invert_yaxis()
    ticks = np.linspace(0, Z.shape[1] - 1, 10).astype(int)
    ax.set_xticks(ticks + 0.5)
    ax.set_xticklabels(dates.strftime("%y-%m")[ticks], fontsize=8)
    ax.set_title(f"{symbol} 状态分位热力图（红=高分位/极端，绿=低分位，灰=闸门失败/样本不足）",
                 fontsize=11)
    return pm


fig, axes = plt.subplots(2, 1, figsize=(15, 9))
for ax, sym in zip(axes, ["SPY", "NVDA"]):
    pm = heatmap(sym, ax)
fig.colorbar(pm, ax=axes, label="252 日滚动分位", pad=0.01, fraction=0.02)
out1 = OUTPUT_DIR / "state_heatmap.png"
fig.savefig(out1, dpi=110, bbox_inches="tight")
print(f"图 1 保存: {out1}")

# --- 图 2：最新交易日三标的状态快照 ---
latest = conn.execute("SELECT MAX(date) FROM rnd_state WHERE pct IS NOT NULL").fetchone()[0]
snap = pd.read_sql_query(
    "SELECT symbol, indicator, pct, dpct FROM rnd_state WHERE date=? AND pct IS NOT NULL",
    conn, params=(latest,))
fig, ax = plt.subplots(figsize=(11, 7))
symbols = ["SPY", "QQQ", "NVDA"]
colors = {"SPY": "#1f77b4", "QQQ": "#2ca02c", "NVDA": "#d62728"}
y = np.arange(len(STATE_INDICATORS))
h = 0.26
for k, sym in enumerate(symbols):
    s = snap[snap["symbol"] == sym].set_index("indicator")["pct"].reindex(STATE_INDICATORS)
    ax.barh(y + (k - 1) * h, s.to_numpy(), height=h, color=colors[sym], label=sym, alpha=0.85)
ax.axvline(50, color="gray", ls="--", lw=1)
ax.axvspan(90, 100, alpha=0.08, color="red")
ax.axvspan(0, 10, alpha=0.08, color="blue")
ax.set_yticks(y)
ax.set_yticklabels([LABELS[i] for i in STATE_INDICATORS])
ax.invert_yaxis()
ax.set_xlim(0, 100)
ax.set_xlabel("252 日滚动分位")
ax.set_title(f"状态快照 {latest}（红带=P90+ 极端，蓝带=P10- 极端）")
ax.legend()
out2 = OUTPUT_DIR / "state_snapshot.png"
fig.savefig(out2, dpi=110, bbox_inches="tight")
print(f"图 2 保存: {out2}")

"""回填完成后的两张核验图：
1. SPY 密度时序热力图（pinned 序列，价格空间，叠现货路径 + roll 刻度）
2. Smile 拟合诊断双联图（健康日 vs 闸门失败日）
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import PowerNorm

from rnd import db
from rnd.config import OUTPUT_DIR
from rnd.pipeline.clean import filter_for_fit, otm_splice
from rnd.pipeline.fit import fit_rnd_no_arb
from rnd.pipeline.forward import implied_forward
from rnd.pipeline.iv import implied_vol

plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti TC", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False
conn = db.get_conn()
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------- 图 1：SPY 密度热力图 ----------
rows = pd.read_sql_query("""
    SELECT i.date, i.forward, i.roll, c.grid_json, r.underlying_close
    FROM rnd_indicators i
    JOIN rnd_curve c USING (date, symbol, expiry)
    JOIN (SELECT DISTINCT date, symbol, underlying_close FROM raw_chain) r
      ON r.date = i.date AND r.symbol = i.symbol
    WHERE i.symbol = 'SPY' AND i.pinned = 1 ORDER BY i.date""", conn)
print(f"SPY pinned 曲线 {len(rows)} 天")

price_grid = np.linspace(340, 860, 420)
Z = np.full((len(price_grid), len(rows)), np.nan)
for j, g in enumerate(rows["grid_json"]):
    grid = json.loads(g)
    Z[:, j] = np.interp(price_grid, grid["strikes"], grid["density"],
                        left=0.0, right=0.0)

dates = pd.to_datetime(rows["date"])
fig, ax = plt.subplots(figsize=(15, 7))
x = np.arange(len(rows))
pm = ax.pcolormesh(x, price_grid, Z, cmap="magma", norm=PowerNorm(0.4),
                   shading="auto")
ax.plot(x, rows["underlying_close"], color="cyan", lw=1.2, label="SPY 收盘")
for j in np.where(rows["roll"] == 1)[0]:
    ax.axvline(j, color="white", lw=0.4, alpha=0.35)
ticks = np.linspace(0, len(rows) - 1, 13).astype(int)
ax.set_xticks(ticks)
ax.set_xticklabels(dates.dt.strftime("%y-%m").iloc[ticks], fontsize=8)
ax.set_ylabel("价格")
ax.set_title("SPY 风险中性密度演化（pinned ≈30DTE 序列，3 年回填）"
             "  白色竖线=roll 日  青线=现货收盘")
fig.colorbar(pm, ax=ax, label="密度", pad=0.01)
ax.legend(loc="upper left")
out1 = OUTPUT_DIR / "heatmap_SPY.png"
fig.savefig(out1, dpi=110, bbox_inches="tight")
print(f"图 1 保存: {out1}")

# ---------- 图 2：smile 诊断双联图 ----------
def rebuild(symbol, date_s, expiry_s):
    chain = pd.read_sql_query(
        "SELECT strike, right, bid, ask, underlying_close, sofr FROM raw_chain"
        " WHERE date=? AND symbol=? AND expiry=?", conn,
        params=(date_s, symbol, expiry_s))
    T = (dt.date.fromisoformat(expiry_s) - dt.date.fromisoformat(date_s)).days / 365
    r = float(chain["sofr"].iloc[0])
    fitted = filter_for_fit(chain[["strike", "right", "bid", "ask"]])
    F, _ = implied_forward(fitted, float(chain["underlying_close"].iloc[0]), r, T)
    otm = otm_splice(fitted, F)
    otm["iv"] = [implied_vol(m, F, k, T, r, rt)
                 for m, k, rt in zip(otm["mid"], otm["strike"], otm["right"])]
    otm = otm.dropna(subset=["iv"])
    x = np.log(otm["strike"].values / F)
    rnd_, checks = fit_rnd_no_arb(x, otm["iv"].values, otm["weight"].values, F, T, r)
    return x, otm["iv"].values, rnd_, checks, F


fail = conn.execute("""SELECT date, expiry FROM rnd_indicators
    WHERE symbol='NVDA' AND json_extract(gate_detail,'$.no_arb')=0
    ORDER BY date DESC LIMIT 1""").fetchone()
cases = [("SPY", "2026-07-10", "2026-08-21", "健康日"),
         ("NVDA", fail[0], fail[1], "闸门失败日 (no_arb)")]

fig, axes = plt.subplots(1, 2, figsize=(15, 5.5))
for ax, (sym, d, e, label) in zip(axes, cases):
    x, iv, rnd_, checks, F = rebuild(sym, d, e)
    sm = rnd_.smile
    xx = np.linspace(x.min() - 0.12, x.max() + 0.12, 400)
    ax.scatter(x, iv, s=12, alpha=0.55, color="#1f77b4", label="IV 散点（清洗后 OTM mid）")
    ax.plot(xx, sm(xx), color="#d62728", lw=1.8, label="平滑样条 + 翼部外推")
    ax.axvspan(xx[0], sm.x_min, alpha=0.12, color="gray")
    ax.axvspan(sm.x_max, xx[-1], alpha=0.12, color="gray", label="外推区")
    ok = "PASS" if checks["all_pass"] else "FAIL"
    ax.set_title(f"{sym} {d} exp {e} — {label}\n"
                 f"报价区无套利 {ok}   rmse={sm.rmse:.4f}  s_mult={sm.s_multiplier:.0f}  "
                 f"n={len(x)}  wing=[{sm._b['lo'][1]:.2f},{sm._b['hi'][1]:.2f}]",
                 fontsize=10)
    ax.set_xlabel("log-moneyness ln(K/F)")
    ax.set_ylabel("IV")
    ax.legend(fontsize=8)
out2 = OUTPUT_DIR / "smile_diagnostic.png"
fig.savefig(out2, dpi=110, bbox_inches="tight")
print(f"图 2 保存: {out2}")

"""第 1 步 Walking skeleton（spec §8.1）：
SPY 一条链 → raw_chain 落库 → 管线（rnd.compute）→ 分位数 → 验收图。

验收基准（spec §8，2026-07-13 符号修正后）：
  1. ±1σ 与 ATM straddle 隐含 expected move 量级一致
  2. sign(F − Q50) 与 25Δ RR（c−p 口径）符号一致

用法：.venv/bin/python scripts/run_skeleton.py [SYMBOL]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from rnd import db, fetch
from rnd.compute import compute_day
from rnd.config import OUTPUT_DIR
from rnd.pipeline.clean import filter_for_fit, quality_flags

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "SPY"
TAIL_PCT = 0.05 if SYMBOL in ("SPY", "QQQ") else 0.10

# ---------- 取数 ----------
asof, underlying_close = fetch.latest_trading_day(SYMBOL)
sofr = fetch.fetch_sofr(asof)
expiries = fetch.monthly_expirations(SYMBOL, asof)
if not expiries:
    sys.exit(f"{SYMBOL}: {asof} 无 7-60 DTE 月度到期")
expiry = min(expiries, key=lambda e: abs((e - asof).days - 30))
dte = (expiry - asof).days
print(f"{SYMBOL} @ {asof}  close={underlying_close}  SOFR={sofr:.4f}")
print(f"到期 {expiry} (DTE {dte})，候选 {[str(e) for e in expiries]}")

raw = fetch.fetch_chain_eod(SYMBOL, expiry, asof)
chain = pd.DataFrame({
    "strike": raw["strike"].astype(float),
    "right": raw["right"].map({"CALL": "C", "PUT": "P"}),
    "bid": raw["bid"].astype(float),
    "ask": raw["ask"].astype(float),
    "volume": raw["volume"].astype(int),
})
conn = db.get_conn()
db.insert_raw_chain(conn, [
    (str(asof), SYMBOL, str(expiry), r.strike, r.right, r.bid, r.ask,
     r.volume, None, quality_flags(r.bid, r.ask), underlying_close, sofr)
    for r in chain.itertuples()
])
print(f"整链 {len(raw)} 行已落库")

# ---------- 管线 ----------
res = compute_day(chain[["strike", "right", "bid", "ask"]], underlying_close,
                  sofr, asof, expiry, SYMBOL, TAIL_PCT)
db.upsert_curve(conn, str(asof), SYMBOL, str(expiry), res.grid, res.fit_meta)
db.upsert_indicators(conn, res.indicators)
ind, rnd = res.indicators, res.rnd
F = ind["forward"]

# ---------- 验收 ----------
fitted = filter_for_fit(chain)
atm_k = fitted.loc[(fitted["strike"] - F).abs().idxmin(), "strike"]
straddle = fitted[fitted["strike"] == atm_k]["mid"].sum()
straddle_sigma = straddle / 0.8            # E|x| = 0.8σ（正态近似）
ratio = ind["sigma1_abs"] / straddle_sigma
acc1 = 0.7 <= ratio <= 1.3
acc2 = np.sign(F - ind["q50"]) == np.sign(ind["rr25"])

print(f"\n===== {SYMBOL} {asof} exp {expiry} =====")
print(f"F={F:.2f}  ATM_IV={ind['atm_iv']:.4f}  ±1σ=±{ind['sigma1_abs']:.2f} "
      f"(±{ind['sigma1_pct']*100:.2f}%)")
for q in ("q05", "q25", "q50", "q75", "q95"):
    print(f"  {q.upper()} = {ind[q]:8.2f}   in_quoted_range={bool(ind[q+'_in_range'])}")
print(f"mode={ind['mode']:.2f}  skew={ind['skew']:.3f}  bowley={ind['bowley_skew']:.4f}  "
      f"ex_kurt={ind['ex_kurt']:.3f}")
print(f"RR25={ind['rr25']:.4f}  BF25={ind['bf25']:.4f}  "
      f"tails: P(−{TAIL_PCT:.0%})={ind['tail_p_down']:.3f} P(+{TAIL_PCT:.0%})={ind['tail_p_up']:.3f}")
print(f"峰: {ind['n_modes']} 个 {ind['modes_json']}")
print(f"无套利: {res.checks}")
print(f"闸门: {'PASS' if ind['gate_pass'] else 'FAIL'} {ind['gate_detail']}")
print(f"\n验收 1（±1σ vs straddle/0.8={straddle_sigma:.2f}，ratio={ratio:.3f}）: "
      f"{'PASS' if acc1 else 'FAIL'}")
print(f"验收 2（sign(F−Q50)={np.sign(F-ind['q50']):+.0f} vs sign(RR25)="
      f"{np.sign(ind['rr25']):+.0f}）: {'PASS' if acc2 else 'FAIL'}")

# ---------- 出图 ----------
OUTPUT_DIR.mkdir(exist_ok=True)
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti TC", "Arial Unicode MS"]
plt.rcParams["axes.unicode_minus"] = False
fig, ax = plt.subplots(figsize=(12, 6.5))
ax.plot(rnd.strikes, rnd.density, lw=2, color="#1f77b4", label="风险中性密度")
k_lo, k_hi = rnd.k_quoted
ax.axvspan(rnd.strikes[0], k_lo, alpha=0.12, color="gray", label="外推区")
ax.axvspan(k_hi, rnd.strikes[-1], alpha=0.12, color="gray")
colors = {"q05": "#d62728", "q25": "#ff7f0e", "q50": "#2ca02c",
          "q75": "#ff7f0e", "q95": "#d62728"}
for q, c in colors.items():
    k, in_r = ind[q], bool(ind[q + "_in_range"])
    ax.axvline(k, color=c, ls="--" if in_r else ":", lw=1.5)
    ax.text(k, ax.get_ylim()[1] * 0.97, f"{q.upper()}\n{k:.0f}",
            ha="center", va="top", fontsize=9, color=c)
ax.axvline(F, color="black", lw=1, alpha=0.6)
ax.text(F, ax.get_ylim()[1] * 0.5, f" F={F:.1f}", fontsize=9)
ax.set_title(f"{SYMBOL} RND  {asof}  exp {expiry} (DTE {dte})  "
             f"ATM IV {ind['atm_iv']:.1%}  ±1σ ±{ind['sigma1_pct']*100:.1f}%  "
             f"闸门 {'PASS' if ind['gate_pass'] else 'FAIL'}")
ax.set_xlabel("行权价")
ax.set_ylabel("密度")
ax.legend(loc="upper left")
ax.set_xlim(F * 0.75, F * 1.2)
out = OUTPUT_DIR / f"skeleton_{SYMBOL}_{asof}.png"
fig.savefig(out, dpi=110, bbox_inches="tight")
print(f"\n图已保存: {out}")

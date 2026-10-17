"""日闸门「有效行权价数」门槛（2026-10-17 校准 25 → 20）+ 历史重判脚本。

纯合成 / 临时库，不触网、不碰生产库。
    .venv/bin/python tests/test_gate_strikes.py
"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np
import pandas as pd

from rnd import db
from rnd.compute import compute_day
from rnd.config import PIPELINE
from rnd.pipeline.iv import black76_price

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# --- 1. compute_day 的门槛 ---
print("[compute_day] OTM 有效档数门槛")
check("PIPELINE['min_otm_strikes'] = 20", PIPELINE.get("min_otm_strikes") == 20,
      str(PIPELINE.get("min_otm_strikes")))

F, R, SIGMA = 100.0, 0.04, 0.30
ASOF, EXPIRY = dt.date(2026, 10, 16), dt.date(2026, 11, 20)
T = (EXPIRY - ASOF).days / PIPELINE["day_count"]


def flat_chain(n_strikes: int) -> pd.DataFrame:
    """平坦 IV 的双边链：80–120 间均匀 n_strikes 档、相对点差 4%（全部过清洗，每档一个 OTM 腿进拟合）。"""
    rows = []
    for k in np.linspace(80, 120, n_strikes):
        for right in ("C", "P"):
            mid = black76_price(F, k, SIGMA, T, R, right)
            rows.append({"strike": round(float(k), 2), "right": right,
                         "bid": mid * 0.98, "ask": mid * 1.02})
    return pd.DataFrame(rows)


for n, expect in ((21, True), (19, False)):
    res = compute_day(flat_chain(n), F * np.exp(-R * T), R, ASOF, EXPIRY, "TEST", 0.10)
    gd = json.loads(res.indicators["gate_detail"])
    used = res.fit_meta["n_strikes_used"]
    check(f"{n} 档链 → n_strikes_ok={expect}", gd["n_strikes_ok"] is expect and used == n,
          f"n_strikes_used={used} gate_detail={gd}")


# --- 2. 历史重判脚本 ---
print("[regate_strikes] 按存量 n_strikes_used 重判，只动 n_strikes_ok / gate_pass")
import regate_strikes  # noqa: E402

OK3 = {"no_arb": True, "rr_skew_agree": True, "fit_rmse_ok": True}
cases = {   # expiry 区分行： (n_used, 其余三项, 原 gate_pass) → 期望新 gate_pass
    "2026-11-20": (22, OK3, 0, 1),                               # 只因档数被挡 → 放行
    "2026-12-18": (22, OK3 | {"no_arb": False}, 0, 0),           # 无套利仍挂 → 仍挡
    "2027-01-15": (18, OK3, 0, 0),                               # 低于新门槛 → 仍挡
    "2027-02-19": (30, OK3, 1, 1),                               # 本就通过 → 不变
}
with tempfile.TemporaryDirectory() as tmp:
    conn = db.get_conn(Path(tmp) / "t.sqlite")
    for exp, (n, others, gp, _) in cases.items():
        detail = {"no_arb": others["no_arb"], "rr_skew_agree": others["rr_skew_agree"],
                  "n_strikes_ok": n >= 25, "fit_rmse_ok": others["fit_rmse_ok"]}
        conn.execute("INSERT INTO rnd_indicators (date, symbol, expiry, gate_pass, gate_detail, pinned)"
                     " VALUES ('2026-10-16', 'TEST', ?, ?, ?, 1)", (exp, gp, json.dumps(detail)))
        conn.execute("INSERT INTO rnd_curve (date, symbol, expiry, grid_json, fit_meta)"
                     " VALUES ('2026-10-16', 'TEST', ?, '{}', ?)", (exp, json.dumps({"n_strikes_used": n})))
    conn.commit()

    stats, changed = regate_strikes.regate(conn, 20, apply=False)
    unchanged = [r[0] for r in conn.execute("SELECT gate_pass FROM rnd_indicators ORDER BY expiry")]
    check("dry-run 不写库", unchanged == [c[2] for c in cases.values()], f"gate_pass={unchanged}")
    check("dry-run 统计：1 行转通过", stats["TEST"]["to_pass"] == 1 and stats["TEST"]["to_fail"] == 0,
          dict(stats["TEST"]))

    stats, changed = regate_strikes.regate(conn, 20, apply=True)
    got = {e: (gp, json.loads(gd)) for e, gp, gd in
           conn.execute("SELECT expiry, gate_pass, gate_detail FROM rnd_indicators")}
    for exp, (n, others, _, want) in cases.items():
        gp, gd = got[exp]
        check(f"{exp}（{n} 档）→ gate_pass={want}",
              gp == want and gd["n_strikes_ok"] is (n >= 20)
              and all(gd[k] == v for k, v in others.items()), f"gate_pass={gp} detail={gd}")
    check("有变化的标的进重建名单", changed == ["TEST"], str(changed))

    stats, changed = regate_strikes.regate(conn, 20, apply=True)
    check("幂等：再跑一次无变化", changed == [] and stats["TEST"]["to_pass"] == 0, dict(stats["TEST"]))
    conn.close()

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过。")

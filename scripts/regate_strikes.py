"""按当前 PIPELINE["min_otm_strikes"] 重判历史行的「有效行权价数」闸门，并重建状态层。

只动派生层（spec §3：派生层可全量重算）：用 rnd_curve.fit_meta 里存的 n_strikes_used
重判 gate_detail.n_strikes_ok，gate_pass = 四项全过；其余三项沿用原值，不重新拟合。
写库后逐个有变化的标的 compute_state，重建 252 日分位（参考样本 = 闸门通过日）。

默认 dry-run 只打印变化；--apply 才写库。动生产库前先 .backup。
    .venv/bin/python scripts/regate_strikes.py [--apply]
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import db
from rnd.config import PIPELINE


def regate(conn, min_strikes: int, *, apply: bool) -> tuple[dict, list[str]]:
    """返回 ({symbol: Counter(rows/to_pass/to_fail/detail_only/skipped)}, 有变化的标的)。"""
    rows = conn.execute(
        "SELECT i.date, i.symbol, i.expiry, i.gate_pass, i.gate_detail,"
        "       json_extract(r.fit_meta, '$.n_strikes_used')"
        " FROM rnd_indicators i JOIN rnd_curve r USING (date, symbol, expiry)").fetchall()
    stats: dict[str, Counter] = defaultdict(Counter)
    updates = []
    for d, sym, exp, gp, gd, n in rows:
        st = stats[sym]
        st["rows"] += 1
        if n is None or not gd:
            st["skipped"] += 1
            continue
        detail = json.loads(gd)
        detail["n_strikes_ok"] = n >= min_strikes
        new_gp = int(all(detail.values()))   # 与 compute_day 同口径：四项全过
        new_gd = json.dumps(detail)
        if new_gd == gd and new_gp == gp:
            continue
        if new_gp != gp:
            st["to_pass" if new_gp else "to_fail"] += 1
        else:
            st["detail_only"] += 1
        updates.append((new_gp, new_gd, d, sym, exp))
    if apply and updates:
        conn.executemany("UPDATE rnd_indicators SET gate_pass=?, gate_detail=?"
                         " WHERE date=? AND symbol=? AND expiry=?", updates)
        conn.commit()
    changed = sorted({u[3] for u in updates})
    return stats, changed


def main():
    apply = "--apply" in sys.argv
    min_strikes = PIPELINE["min_otm_strikes"]
    conn = db.get_conn()
    stats, changed = regate(conn, min_strikes, apply=apply)
    print(f"min_otm_strikes={min_strikes}  {'APPLY' if apply else 'DRY-RUN'}")
    for sym in sorted(stats):
        st = stats[sym]
        print(f"  {sym:6} rows={st['rows']:5}  →通过 {st['to_pass']:4}  →不通过 {st['to_fail']:4}"
              f"  仅明细 {st['detail_only']:4}  跳过 {st['skipped']}")
    if apply:
        from rnd.state import compute_state
        for sym in changed:
            print(f"  重建状态层 {sym}: {compute_state(conn, sym)} 行", flush=True)
    conn.close()
    print("完成。" if apply else "dry-run 完成（加 --apply 写库）。")


if __name__ == "__main__":
    main()

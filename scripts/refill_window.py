"""按日期区间补拉 + 重算（到期窗口变更后的补丁工具）。

场景：改了 symbols.yaml 的 expiry.dte_min / dte_max 之后，历史上被旧窗口滤掉的
(日, 到期) 组合需要补回来。scripts/backfill.py 是按「标的 + 年数」整段回填的，
重跑会把整个 3 年窗口每个到期都重新拉一遍（续跑跳过检查按新窗口的 expected_last
判定，必然 miss）——补几天数据不值当。本脚本把区间收窄到指定的几天。

复用既有函数，不另起计算逻辑：
  backfill.ingest_symbol → raw_chain（INSERT OR IGNORE）
  backfill.compute_symbol → 管线 + rnd_indicators（跳过已算行）
  timeseries.postprocess_symbol → pinned / roll / term_slope
  state.compute_state → 252 日滚动分位

幂等可反复重跑。用法：
  .venv/bin/python scripts/refill_window.py --start 2026-09-12 --end 2026-09-16
  .venv/bin/python scripts/refill_window.py --start ... --end ... --symbols QQQ,SPY
"""
import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backfill import DTE_MAX, DTE_MIN, compute_symbol, ingest_symbol
from rnd import config, db, fetch
from rnd.state import compute_state
from rnd.timeseries import postprocess_symbol


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="区间起始日 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="区间结束日 YYYY-MM-DD（超出美东今天会被钳回）")
    ap.add_argument("--symbols", default="", help="默认取有效池")
    ap.add_argument("--skip-fetch", action="store_true", help="只重算，不拉数据")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        from server import pool
        symbols = pool.effective_symbols()

    print(f"补拉区间 {start} → {end}（美东今天 {fetch.market_today()}）")
    print(f"到期窗口 DTE ∈ [{DTE_MIN}, {DTE_MAX}]（symbols.yaml → config.EXPIRY={config.EXPIRY}）")
    print(f"标的 {symbols}")

    conn = db.get_conn()
    # SOFR 多取几天，ingest 里按交易日历 reindex+ffill，区间左端要有值可前向填充
    sofr = fetch.fetch_sofr_series(start - dt.timedelta(days=10), end)
    print(f"SOFR 序列 {len(sofr)} 天（{sofr.index[0]} → {sofr.index[-1]}）")

    failed = []
    for symbol in symbols:
        print(f"\n===== {symbol} =====")
        try:   # 单标的失败不中断其它标的
            if not args.skip_fetch:
                ingest_symbol(conn, symbol, start, end, sofr)
            compute_symbol(conn, symbol, recompute=False)
            postprocess_symbol(conn, symbol)
            n_state = compute_state(conn, symbol)
            print(f"  {symbol}: 状态层 {n_state} 行")
        except Exception as e:   # noqa: BLE001
            failed.append(symbol)
            print(f"  {symbol}: 失败，跳过（{type(e).__name__}: {str(e)[:150]}）")
    conn.close()
    print(f"\n完成。{'失败: ' + ', '.join(failed) if failed else '全部成功。'}")


if __name__ == "__main__":
    main()

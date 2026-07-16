"""第 4 步：为所有标的构建 252 日滚动分位状态层。

用法：.venv/bin/python scripts/build_state.py [--symbols SPY,QQQ,NVDA]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import db
from rnd.state import MIN_SAMPLE, STATE_INDICATORS, WINDOW, compute_state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ,NVDA")
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",")]

    conn = db.get_conn()
    print(f"状态层：window={WINDOW} min_sample={MIN_SAMPLE}  指标 {len(STATE_INDICATORS)} 个")
    for symbol in symbols:
        n = compute_state(conn, symbol)
        # 首个出分位的日期（冷启动消失点）
        first = conn.execute(
            "SELECT MIN(date) FROM rnd_state WHERE symbol=? AND pct IS NOT NULL",
            (symbol,)).fetchone()[0]
        scored = conn.execute(
            "SELECT COUNT(*) FROM rnd_state WHERE symbol=? AND pct IS NOT NULL",
            (symbol,)).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM rnd_state WHERE symbol=?", (symbol,)).fetchone()[0]
        print(f"  {symbol}: {n} 行，出分位 {scored}/{total}，首个分位日 {first}")
    print("\n完成。")


if __name__ == "__main__":
    main()

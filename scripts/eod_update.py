"""每日 EOD 增量更新：追平 raw_chain 缺口 → 管线 → pinned/roll → 状态层。

幂等：raw 层 INSERT OR IGNORE，指标层跳过已算行。无新交易日时直接退出。
本地手动跑或 cron（美股收盘后，东京时间约 07:00）：
    .venv/bin/python scripts/eod_update.py
"""
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from rnd import db, fetch
from rnd.state import compute_state
from rnd.timeseries import postprocess_symbol
from rnd.pipeline.clean import quality_flags

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill import compute_symbol, with_retry  # 复用计算循环与重试


def update_symbol(conn, symbol: str, sofr: pd.Series, today: dt.date) -> int:
    last = conn.execute("SELECT MAX(date) FROM raw_chain WHERE symbol=?",
                        (symbol,)).fetchone()[0]
    start = dt.date.fromisoformat(last) + dt.timedelta(days=1)
    if start > today:
        print(f"  {symbol}: 已是最新（{last}）")
        return 0
    stock = with_retry(lambda: fetch.stock_history_eod_chunked(symbol, start, today),
                       label=f"{symbol} stock")
    if stock.empty:
        print(f"  {symbol}: {last} 之后无新交易日")
        return 0
    stock = stock.assign(date=pd.to_datetime(stock["created"]).dt.date)
    closes = dict(zip(stock["date"], stock["close"].astype(float)))
    days = sorted(closes)
    sofr_daily = sofr.reindex(days).ffill().bfill()
    print(f"  {symbol}: 新交易日 {len(days)} 天（{days[0]} → {days[-1]}）")

    exps = with_retry(lambda: fetch._client().option_list_expirations(symbol=symbol),
                      label=f"{symbol} expirations")
    exp_dates = sorted(pd.to_datetime(exps["expiration"]).dt.date)
    listed = set(exp_dates)
    # 新交易日窗口内可能被钉到的月度：任一新交易日的 DTE ∈ [7,60]
    monthlies = [e for e in exp_dates if fetch.is_monthly(e, listed)
                 and days[0] + dt.timedelta(days=7) <= e <= days[-1] + dt.timedelta(days=60)]
    n_rows = 0
    for expiry in monthlies:
        s = max(expiry - dt.timedelta(days=60), days[0])
        e = min(expiry - dt.timedelta(days=7), days[-1])
        if s > e:
            continue
        t0 = time.time()
        raw = with_retry(
            lambda: fetch._client().option_history_eod(
                start_date=s, end_date=e, symbol=symbol, expiration=expiry),
            label=f"{symbol} {expiry}")
        raw = raw.assign(date=pd.to_datetime(raw["created"]).dt.date)
        rows = []
        for r in raw.itertuples():
            if r.date not in closes:
                continue
            bid, ask = float(r.bid), float(r.ask)
            rows.append((str(r.date), symbol, str(expiry), float(r.strike),
                         "C" if r.right == "CALL" else "P", bid, ask,
                         int(r.volume), None, quality_flags(bid, ask),
                         closes[r.date], float(sofr_daily.loc[r.date])))
        db.insert_raw_chain(conn, rows)
        n_rows += len(rows)
        print(f"    {expiry}: {len(rows)} 行 ({time.time()-t0:.0f}s)")
    compute_symbol(conn, symbol, recompute=False)
    postprocess_symbol(conn, symbol)
    compute_state(conn, symbol)
    return n_rows


def main():
    today = dt.date.today()
    conn = db.get_conn()
    symbols = [s.strip() for s in
               (sys.argv[1].split(",") if len(sys.argv) > 1 else ["SPY", "QQQ", "NVDA"])]
    lo = min(dt.date.fromisoformat(
        conn.execute("SELECT MAX(date) FROM raw_chain WHERE symbol=?", (s,)).fetchone()[0])
        for s in symbols)
    sofr = fetch.fetch_sofr_series(lo - dt.timedelta(days=7), today)
    print(f"EOD 增量更新 @ {today}")
    for sym in symbols:
        update_symbol(conn, sym, sofr, today)
    # roll 日重钉（spec §6）：自愈式，漏跑几天也会在下次运行补上
    from rnd.journal import roll_repin_check
    for r in roll_repin_check(conn):
        print(f"  roll_repin: {r['symbol']} {r['position_id']} {r['from']} → {r['to']}")

    # TG 推送（framework §2.3）：异动 + 综合摘要。失败/未配不影响数据更新。
    conn.close()
    try:
        from push_daily import run as push_run
        from rnd.telegram import TelegramNotConfigured
        print("推送:", push_run(symbols) or "无内容")
    except TelegramNotConfigured as e:
        print(f"推送: 跳过（{e}）")
    except Exception as e:  # noqa: BLE001
        print(f"推送: 失败但不影响数据（{type(e).__name__}: {e}）")
    print("完成。")


if __name__ == "__main__":
    main()

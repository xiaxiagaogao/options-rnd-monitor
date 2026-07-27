"""第 3 步：历史回填（spec §8.3）。

每标的：枚举窗口内月度到期 → 每到期一次范围请求拉整段链 → raw_chain 落库
→ 逐 (日, 到期) 跑管线 → pinned/roll/term_slope 后处理。

幂等可续跑：raw 层 INSERT OR IGNORE；指标层默认跳过已算行（--recompute 强制重算）。

用法：.venv/bin/python scripts/backfill.py [--symbols SPY,QQQ,NVDA] [--years 3] [--recompute]
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from thetadata.errors import NoDataFoundError

from rnd import db, fetch
from rnd.compute import ComputeError, compute_day
from rnd.timeseries import postprocess_symbol
from rnd.pipeline.clean import quality_flags

INDEX_LIKE = {"SPY", "QQQ"}          # 尾部概率口径：指数 ±5%，个股 ±10%（spec §4）


def with_retry(fn, tries=4, base=5, label=""):
    for i in range(tries):
        try:
            return fn()
        except NoDataFoundError:
            raise   # 数据不存在是确定性结果（上市前/无此到期），重试无用，交调用方处理
        except Exception as e:
            if i == tries - 1:
                raise
            wait = base * 2**i
            print(f"    重试 {label}（{i+1}/{tries-1}，{wait}s 后）: {str(e)[:120]}")
            time.sleep(wait)


def ingest_symbol(conn, symbol: str, start: dt.date, end: dt.date, sofr: pd.Series):
    stock = with_retry(
        lambda: fetch.stock_history_eod_chunked(symbol, start, end),
        label=f"{symbol} stock")
    if stock.empty:
        print(f"  {symbol}: 窗口内无股票数据（整段在上市前？），跳过")
        return
    stock = stock.assign(date=pd.to_datetime(stock["created"]).dt.date)
    closes = dict(zip(stock["date"], stock["close"].astype(float)))
    calendar = sorted(closes)
    sofr_daily = sofr.reindex(calendar).ffill().bfill()

    exps = with_retry(lambda: fetch._client().option_list_expirations(symbol=symbol),
                      label=f"{symbol} expirations")
    exp_dates = sorted(pd.to_datetime(exps["expiration"]).dt.date)
    listed = set(exp_dates)
    monthlies = [e for e in exp_dates
                 if fetch.is_monthly(e, listed)
                 and start + dt.timedelta(days=7) <= e <= end + dt.timedelta(days=60)]
    print(f"  {symbol}: {len(monthlies)} 个月度到期，交易日历 {len(calendar)} 天")

    for n, expiry in enumerate(monthlies, 1):
        s = max(expiry - dt.timedelta(days=60), start)
        e = min(expiry - dt.timedelta(days=7), end)
        if s > e:
            continue
        # 续跑：该到期在窗口内的最后交易日已入库则跳过拉取
        expected_last = max(d for d in calendar if d <= e)
        done = conn.execute(
            "SELECT 1 FROM raw_chain WHERE symbol=? AND expiry=? AND date=? LIMIT 1",
            (symbol, str(expiry), str(expected_last))).fetchone()
        if done:
            print(f"  [{n}/{len(monthlies)}] {expiry} 已入库，跳过")
            continue
        t0 = time.time()
        try:
            raw = with_retry(
                lambda: fetch.fetch_chain_eod(symbol, expiry, s) if s == e else
                fetch._client().option_history_eod(start_date=s, end_date=e,
                                                   symbol=symbol, expiration=expiry),
                label=f"{symbol} {expiry}")
        except NoDataFoundError:
            print(f"  [{n}/{len(monthlies)}] {expiry}: 无期权数据（上市前/未挂牌），跳过")
            continue
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
        print(f"  [{n}/{len(monthlies)}] {expiry}: {len(rows)} 行 ({time.time()-t0:.0f}s)")
        time.sleep(1)


def compute_symbol(conn, symbol: str, recompute: bool):
    tail_pct = 0.05 if symbol in INDEX_LIKE else 0.10
    skip = "" if recompute else (
        " AND NOT EXISTS (SELECT 1 FROM rnd_indicators i"
        "  WHERE i.date = r.date AND i.symbol = r.symbol AND i.expiry = r.expiry)")
    pairs = conn.execute(
        f"SELECT DISTINCT r.date, r.expiry FROM raw_chain r WHERE r.symbol = ?{skip}"
        " ORDER BY r.date, r.expiry", (symbol,)).fetchall()
    # 过滤发生在计算时（spec §3）：raw 层可能残留早期误入的 weekly，不参与计算
    listed = {dt.date.fromisoformat(r[0]) for r in conn.execute(
        "SELECT DISTINCT expiry FROM raw_chain WHERE symbol = ?", (symbol,))}
    pairs = [(d, e) for d, e in pairs
             if fetch.is_monthly(dt.date.fromisoformat(e), listed)]
    print(f"  {symbol}: 待计算 {len(pairs)} 个 (日, 到期)")
    ok = fail = 0
    failures = []
    t0 = time.time()
    for i, (date_s, expiry_s) in enumerate(pairs, 1):
        chain = pd.read_sql_query(
            "SELECT strike, right, bid, ask, underlying_close, sofr FROM raw_chain"
            " WHERE date=? AND symbol=? AND expiry=?",
            conn, params=(date_s, symbol, expiry_s))
        try:
            res = compute_day(chain[["strike", "right", "bid", "ask"]],
                              float(chain["underlying_close"].iloc[0]),
                              float(chain["sofr"].iloc[0]),
                              dt.date.fromisoformat(date_s),
                              dt.date.fromisoformat(expiry_s), symbol, tail_pct)
            db.upsert_curve(conn, date_s, symbol, expiry_s, res.grid, res.fit_meta)
            db.upsert_indicators(conn, res.indicators)
            ok += 1
        except ComputeError as e:
            fail += 1
            failures.append((date_s, expiry_s, str(e)))
        if i % 100 == 0:
            print(f"    {i}/{len(pairs)} ({time.time()-t0:.0f}s)")
    print(f"  {symbol}: 计算完成 {ok} OK / {fail} FAIL ({time.time()-t0:.0f}s)")
    for f in failures[:10]:
        print(f"    跳过 {f[0]} {f[1]}: {f[2]}")
    if len(failures) > 10:
        print(f"    …共 {len(failures)} 个跳过")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SPY,QQQ,NVDA")
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--recompute", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true", help="只重算，不拉数据")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    end, _ = fetch.latest_trading_day("SPY")
    start = end - dt.timedelta(days=int(args.years * 365))
    print(f"回填窗口 {start} → {end}，标的 {symbols}")

    conn = db.get_conn()
    sofr = fetch.fetch_sofr_series(start, end)
    print(f"SOFR 序列 {len(sofr)} 天（{sofr.index[0]} → {sofr.index[-1]}）")

    from rnd.state import compute_state
    failed = []
    for symbol in symbols:
        print(f"\n===== {symbol} =====")
        try:   # 单标的失败（如某标的链数据异常）不中断其它标的的回填
            if not args.skip_fetch:
                ingest_symbol(conn, symbol, start, end, sofr)
            compute_symbol(conn, symbol, args.recompute)
            postprocess_symbol(conn, symbol)
            compute_state(conn, symbol)
            n = conn.execute("SELECT COUNT(*), SUM(gate_pass) FROM rnd_indicators"
                             " WHERE symbol=?", (symbol,)).fetchone()
            print(f"  {symbol}: rnd_indicators {n[0]} 行，闸门通过 {n[1]}")
        except Exception as e:   # noqa: BLE001
            failed.append(symbol)
            print(f"  {symbol}: 回填失败，跳过（{type(e).__name__}: {str(e)[:150]}）")
    print(f"\n回填完成。{'失败: ' + ', '.join(failed) if failed else '全部成功。'}")


if __name__ == "__main__":
    main()

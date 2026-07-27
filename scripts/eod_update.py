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
from thetadata.errors import NoDataFoundError

from rnd import db, fetch
from rnd.state import compute_state
from rnd.timeseries import postprocess_symbol
from rnd.pipeline.clean import quality_flags

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill import compute_symbol, with_retry  # 复用计算循环与重试


def _spawn_backfill(symbols: list[str]):
    """一个后台进程串行回填所有新标的。ThetaData 免费档单终端会话，绝不能多进程
    并发拉（会 Invalid session ID 崩）。故：①一次 spawn 一个进程、内部串行遍历；
    ②由调用方放在 eod_update 增量之后 spawn——此时主进程即将退出、ThetaData 空闲，
    backfill 独占，不与增量抢会话。"""
    import subprocess
    root = Path(__file__).resolve().parent.parent
    log = root / "output" / f"backfill_holdings_{dt.date.today().isoformat()}.log"
    log.parent.mkdir(exist_ok=True)
    with open(log, "ab") as fh:
        subprocess.Popen(
            [sys.executable, str(root / "scripts" / "backfill.py"),
             "--symbols", ",".join(symbols), "--years", "3"],
            stdout=fh, stderr=subprocess.STDOUT, cwd=root, start_new_session=True)
    print(f"    回填派发（串行）: {', '.join(symbols)}（后台，日志 {log.name}）")


def _notify_holdings(res: dict):
    # 仅在有新纳入或闸门出错时推送（避免每日重复告警持有的黑名单标的）
    if not (res.get("added") or res.get("gate_errors")):
        return
    from rnd import telegram
    from rnd.holdings_sync import SYMBOL_BLACKLIST
    lines = []
    if res.get("added"):
        lines.append("新纳入标的（持仓同步）: " + ", ".join(res["added"]))
    if res.get("gate_errors"):
        lines.append("闸门检查出错（下次重试）: " + ", ".join(res["gate_errors"]))
    if res.get("excluded"):
        parts = [f"{s}（{SYMBOL_BLACKLIST.get(s, '未映射')}）" for s in res["excluded"]]
        lines.append("持仓中未纳入: " + ", ".join(parts))
    try:
        telegram.send("【持仓同步】\n" + "\n".join(lines))
    except telegram.TelegramNotConfigured:
        pass


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
        try:
            raw = with_retry(
                lambda: fetch._client().option_history_eod(
                    start_date=s, end_date=e, symbol=symbol, expiration=expiry),
                label=f"{symbol} {expiry}")
        except NoDataFoundError:
            print(f"  {symbol} {expiry}: 无期权数据，跳过")
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
        n_rows += len(rows)
        print(f"    {expiry}: {len(rows)} 行 ({time.time()-t0:.0f}s)")
    compute_symbol(conn, symbol, recompute=False)
    postprocess_symbol(conn, symbol)
    compute_state(conn, symbol)
    return n_rows


def main():
    today = dt.date.today()
    conn = db.get_conn()

    # 持仓同步（holdings-sync）：先跑，用最新持仓驱动池。失败不阻断数据。
    # sync/backfill 派发/notify 三段独立兜底：sync 成功后，backfill 或 notify 出错
    # 不可误报"sync 失败"（否则明天不再把该标的当 added，永久卡在 MAX(date) IS NULL）。
    from rnd import holdings_sync
    from server import pool
    added_syms: list[str] = []   # 新纳入标的，回填延到增量之后串行派发（见文末，避免抢 ThetaData 会话）
    try:
        res = holdings_sync.sync(conn)
    except Exception as e:  # noqa: BLE001
        print(f"持仓同步: sync 失败但不影响数据（{type(e).__name__}: {e}）")
        res = None
    if res is not None:
        if "skipped" in res:
            print(f"持仓同步: {res['skipped']}")
        else:
            print(f"持仓同步: +{res['added']} -{res['removed']} pin={res['pinned']} "
                  f"拒={res['rejected']} 排除={res['excluded']} 错={res.get('gate_errors', [])}")
            added_syms = res.get("added", [])
            try:
                _notify_holdings(res)
            except Exception as e:  # noqa: BLE001
                print(f"  持仓同步告警失败（{type(e).__name__}: {e}）")

    # 更新对象 = 有效池（argv 显式指定时仍尊重）；但**只更新已有数据的标的**——
    # 新 added 标的当天还在后台 backfill、raw_chain 无数据，MAX(date) 为 None，
    # 必须排除出增量循环（否则 dt.date.fromisoformat(None) 崩）。
    if len(sys.argv) > 1:
        requested = [s.strip() for s in sys.argv[1].split(",")]
    else:
        requested = pool.effective_symbols()
    symbols = [s for s in requested
               if conn.execute("SELECT MAX(date) FROM raw_chain WHERE symbol=?",
                               (s,)).fetchone()[0] is not None]
    skipped_new = [s for s in requested if s not in symbols]
    if skipped_new:
        print(f"  本次跳过（无数据，backfill 中）: {skipped_new}")
    if not symbols:
        print("无已落库标的可增量更新。")
        conn.close()
        if added_syms:   # 仍要回填新标的（此路径无增量、ThetaData 空闲）
            _spawn_backfill(added_syms)
        return

    lo = min(dt.date.fromisoformat(
        conn.execute("SELECT MAX(date) FROM raw_chain WHERE symbol=?", (s,)).fetchone()[0])
        for s in symbols)
    sofr = fetch.fetch_sofr_series(lo - dt.timedelta(days=7), today)
    print(f"EOD 增量更新 @ {today}")
    for sym in symbols:
        try:   # 单标的增量失败不中断其它标的，也不阻断后续 roll/push
            update_symbol(conn, sym, sofr, today)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: 增量更新失败，跳过（{type(e).__name__}: {str(e)[:120]}）")
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

    # 新标的串行回填：放在增量+推送之后 spawn——此时主进程即将退出、ThetaData 空闲，
    # backfill 独占单会话，不与增量并发抢连接（否则 Invalid session ID 崩）。
    if added_syms:
        try:
            _spawn_backfill(added_syms)
        except Exception as e:  # noqa: BLE001
            print(f"  回填派发失败（{type(e).__name__}: {e}）")
    print("完成。")


if __name__ == "__main__":
    main()

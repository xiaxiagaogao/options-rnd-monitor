"""每日 EOD 增量更新：追平 raw_chain 缺口 → 管线 → pinned/roll → 状态层。

幂等：raw 层 INSERT OR IGNORE，指标层跳过已算行。无新交易日时直接退出。
数据源尚未发布当天 EOD 的标的（落后于参照交易日）当次隔 25 分钟补拉，最多 3 次，
补完再推送；休市日不推送（见 run_incremental / should_push）。
本地手动跑或 cron（美东 18:00，ThetaData 17:15 ET 出全国 EOD；东京夏令 07:00 / 冬令 08:00）：
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
from backfill import DTE_MAX, DTE_MIN, compute_symbol, with_retry  # 复用计算循环与重试


REFILL_WAIT_MIN = 25   # 当次补拉间隔（分钟）
REFILL_TRIES = 3       # 补拉次数上限。总窗口 ≤ 75 分钟是硬约束：market-agent 07:40 SGT
                       # 读 RND 读数（夏令时补拉最晚约 23:15 UTC 收尾），拉长须同步挪它的 cron。


def _latest(conn, symbol: str) -> str | None:
    return conn.execute("SELECT MAX(date) FROM raw_chain WHERE symbol=?",
                        (symbol,)).fetchone()[0]


def _now_et() -> dt.datetime:
    return dt.datetime.now(fetch.MARKET_TZ)


def update_symbol(conn, symbol: str, sofr: pd.Series, today: dt.date) -> int:
    last = _latest(conn, symbol)
    start = dt.date.fromisoformat(last) + dt.timedelta(days=1)
    if start > today:
        print(f"  {symbol}: 已是最新（{last}）")
        return 0
    stock = with_retry(lambda: fetch.stock_history_eod_chunked(symbol, start, today),
                       label=f"{symbol} stock")
    if stock.empty:
        # 休市还是数据源尚未发布，要等全池跑完、对照参照日才分得清（run_incremental）
        print(f"  {symbol}: {last} 之后数据源暂无数据")
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
    # 新交易日窗口内可能被钉到的月度：任一新交易日的 DTE ∈ [DTE_MIN, DTE_MAX]
    monthlies = [e for e in exp_dates if fetch.is_monthly(e, listed)
                 and days[0] + dt.timedelta(days=DTE_MIN) <= e <= days[-1] + dt.timedelta(days=DTE_MAX)]
    n_rows = 0
    for expiry in monthlies:
        s = max(expiry - dt.timedelta(days=DTE_MAX), days[0])
        e = min(expiry - dt.timedelta(days=DTE_MIN), days[-1])
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


def reference_day(conn, symbols: list[str], today: dt.date,
                  now: dt.datetime) -> tuple[str | None, bool | None]:
    """(参照交易日, 今天是否交易日)；后者 None = 没问到日历。

    参照日 = 本池已落库的最新数据日：任一标的拿到了 D，D 就是数据源已出的交易日，
    落后于它又拉到空表的标的 = 数据源尚未发布，而非休市（2026-10-01：SPY 有、QQQ 无，
    两种「空」原先走同一条路，被打成「无新交易日」静默落后一天）。
    全池都没拿到今天时再问数据源日历：今天开市且已收盘 → 参照日取今天（数据源整体
    未发布，如冬令时 cron 早于 17:15 ET 出 EOD）。日历拿不到 → 按全空 = 无新交易日。
    """
    ref = max(filter(None, (_latest(conn, s) for s in symbols)), default=None)
    if ref is not None and ref >= today.isoformat():
        return ref, True
    try:
        session = fetch.market_session_today()
    except Exception as e:  # noqa: BLE001
        print(f"  交易日历查询失败（{type(e).__name__}: {str(e)[:120]}），按无新交易日处理")
        return ref, None
    trading = session.get("type") in fetch.TRADING_SESSION_TYPES
    if not trading:
        print(f"  交易日历：今天 {session.get('type')}")
    elif now.time() >= dt.time.fromisoformat(session.get("close") or "16:00:00"):
        ref = today.isoformat()
    return ref, trading


def _refill(conn, pending: list[str], sofr: pd.Series, today: dt.date, ref: str) -> list[str]:
    """尚未发布的标的隔 REFILL_WAIT_MIN 分钟重拉，最多 REFILL_TRIES 次；返回仍未补上的。

    主进程内串行（ThetaData 单会话，不能另起进程并发拉）。补不上也不要紧：
    下次运行从 MAX(date)+1 起拉，自然补上。"""
    errored: list[str] = []
    for i in range(1, REFILL_TRIES + 1):
        print(f"补拉 {i}/{REFILL_TRIES}：{REFILL_WAIT_MIN} 分钟后重试 {' / '.join(pending)}")
        sys.stdout.flush()   # cron 重定向到文件是块缓冲，等待期间 tail eod.log 要看得到
        time.sleep(REFILL_WAIT_MIN * 60)
        still = []
        for sym in pending:
            try:
                update_symbol(conn, sym, sofr, today)
            except Exception as e:  # noqa: BLE001
                print(f"  {sym}: 补拉失败，放弃（{type(e).__name__}: {str(e)[:120]}）")
                errored.append(sym)
                continue
            if (_latest(conn, sym) or "") < ref:
                still.append(sym)
        done = [s for s in pending if s not in still and s not in errored]
        if done:
            print(f"补拉 {i}/{REFILL_TRIES}：已补齐 {' / '.join(done)}")
        pending = still
        if not pending:
            break
    if pending:
        print(f"补拉 {REFILL_TRIES} 次仍未发布：{' / '.join(pending)}"
              f"（停在 {', '.join(str(_latest(conn, s)) for s in pending)}；下次运行自愈）")
    return pending + errored


def run_incremental(conn, symbols: list[str], sofr: pd.Series, today: dt.date, *,
                    now: dt.datetime | None = None) -> dict:
    """增量 → 判定尚未发布 → 当次补拉。返回推送决策要用的：ref 参照交易日 / trading 今天
    是否交易日（None=未知）/ got_new 本次有无新数据 / pending 判为尚未发布的 / missing 补拉后仍缺的。"""
    before = {s: _latest(conn, s) for s in symbols}
    failed = set()
    for sym in symbols:
        try:   # 单标的增量失败不中断其它标的，也不阻断后续 roll/push
            update_symbol(conn, sym, sofr, today)
        except Exception as e:  # noqa: BLE001
            failed.add(sym)
            print(f"  {sym}: 增量更新失败，跳过（{type(e).__name__}: {str(e)[:120]}）")
    ref, trading = reference_day(conn, symbols, today, now or _now_et())
    # 拉取异常的不进补拉：确定性错误会把推送白白拖满整个补拉窗口；下次运行自愈
    pending = [s for s in symbols
               if s not in failed and ref and (_latest(conn, s) or "") < ref]
    missing: list[str] = []
    if pending:
        print(f"  数据源尚未发布 {ref}："
              + "、".join(f"{s}（停在 {_latest(conn, s)}）" for s in pending))
        missing = _refill(conn, pending, sofr, today, ref)
    got_new = any(_latest(conn, s) != before[s] for s in symbols)
    if not got_new and not pending:
        print(f"  {ref} 之后无新交易日")
    return {"ref": ref, "trading": trading, "got_new": got_new,
            "pending": pending, "missing": missing}


def should_push(res: dict) -> bool:
    """休市日（日历判定今天不开市，且全池都没有新数据）不推送——不再把上一交易日的
    异动 + 展望重推一遍。日历拿不到（trading=None）照推：宁可多推也别静默。"""
    return not (res["trading"] is False and not res["got_new"])


def main():
    """整个运行期间持 ThetaData 锁（与自选清单 worker 的准入 / 回填互斥）；结束后若清单还有
    待处理条目（准入 / 回填排队或上次出错），起一个 worker 接着做。"""
    from rnd.thetalock import thetadata_lock
    try:
        with thetadata_lock(blocking=False):
            _run()
    except BlockingIOError:
        print("ThetaData 锁被占用（自选清单 worker 在准入 / 回填），等它结束 …", flush=True)
        with thetadata_lock():
            _run()
    _spawn_watchlist_worker()


def _spawn_watchlist_worker():
    from rnd import watchlist, watchlist_worker
    try:
        conn = db.get_conn()
        try:
            pending = watchlist.pending_jobs(conn)
        finally:
            conn.close()
        if pending:
            watchlist_worker.spawn()
            print(f"自选清单: 待处理 {pending}，已起 worker（日志 output/watchlist_worker.log）")
    except Exception as e:  # noqa: BLE001
        print(f"自选清单: worker 派发失败（{type(e).__name__}: {e}）")


def _run():
    # 增量窗口的"今天"取美东（VPS 在 +08，本机日期是美东的明天 → ThetaData 拒收未来
    # end_date，2026-09-01 事故根因）。
    today = fetch.market_today()
    conn = db.get_conn()
    from server import pool

    # 池 = 自选清单里 ready 的标的（watchlist spec，2026-11-07）。持仓同步不再改池，
    # 新标的的准入与回填由自选清单 worker 负责，这里不再派发回填。
    # 更新对象 = 有效池（argv 显式指定时仍尊重）；但**只更新已有数据的标的**——
    # 无 raw_chain 的标的 MAX(date) 为 None，必须排除出增量循环（否则 fromisoformat(None) 崩）。
    if len(sys.argv) > 1:
        requested = [s.strip() for s in sys.argv[1].split(",")]
    else:
        requested = pool.effective_symbols()
    symbols = [s for s in requested if _latest(conn, s) is not None]
    skipped_new = [s for s in requested if s not in symbols]
    if skipped_new:
        print(f"  本次跳过（无数据）: {skipped_new}")
    if not symbols:
        print("无已落库标的可增量更新。")
        conn.close()
        return

    lo = min(dt.date.fromisoformat(_latest(conn, s)) for s in symbols)
    sofr = fetch.fetch_sofr_series(lo - dt.timedelta(days=7), today)
    print(f"EOD 增量更新 @ {today}")
    res = run_incremental(conn, symbols, sofr, today)
    # roll 日重钉（spec §6）：自愈式，漏跑几天也会在下次运行补上
    from rnd.journal import roll_repin_check
    for r in roll_repin_check(conn):
        print(f"  roll_repin: {r['symbol']} {r['position_id']} {r['from']} → {r['to']}")

    # TG 推送（framework §2.3）：异动 + 综合摘要。失败/未配不影响数据更新。
    # 放在补拉之后：落后日整条推送推迟到补拉结束（2026-10-02 定——美股东京 22:30 才开盘，
    # 07:00 与 08:15 收到决策上无差别；一条完整推送胜过混日期展望 + 补齐短讯）。
    conn.close()
    if not should_push(res):
        print("推送: 休市日不推（数据源日历判定今天不开市，且全池无新数据）")
    else:
        try:
            from push_daily import run as push_run
            from rnd.telegram import TelegramNotConfigured
            print("推送:", push_run(symbols, ref_day=res["ref"]) or "无内容")
        except TelegramNotConfigured as e:
            print(f"推送: 跳过（{e}）")
        except Exception as e:  # noqa: BLE001
            print(f"推送: 失败但不影响数据（{type(e).__name__}: {e}）")

    print("完成。")


if __name__ == "__main__":
    main()

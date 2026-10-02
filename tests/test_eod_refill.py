"""EOD「数据源尚未发布」判定 + 当次补拉（2026-10-01 事故防复发）。

事故形态：cron 18:00 ET 跑时 ThetaData 已给出 SPY 等 6 只的 10-01 EOD，QQQ/AVGO/SNDK
却还是 NoData（报告 created 都是 17:15 ET，生成了但没对外服务）。update_symbol 把空表
一律打成「无新交易日」return 0——「数据源还没出」与「休市」走同一条路，三只停在 09-30。

口径：参照交易日 = 本池已落库的最新数据日；全体都没拿到当天时再问数据源日历
（calendar_open_today，免费档唯一可用的日历端点）。落后于参照日、且不是拉取异常的标的
= 尚未发布 → 隔 REFILL_WAIT_MIN 分钟补拉，最多 REFILL_TRIES 次；补齐后再推送。
休市日（日历判定不开市，且全体都没有新数据）不推送。

真 SQLite（内存，正式 DDL）+ 假 ThetaData，不实拉网络、不读真库：
    .venv/bin/python tests/test_eod_refill.py
"""
import contextlib
import io
import sqlite3
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import datetime as dt

import pandas as pd
from thetadata.errors import NoDataFoundError

from rnd import db, fetch
import eod_update

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


REF = dt.date(2026, 10, 1)                          # 周四，交易日
PREV = "2026-09-30"                                 # 库内各标的停在这天
EXPIRIES = [dt.date(2026, 10, 16), dt.date(2026, 11, 20)]   # 第三个周五 = 月度
AFTER_CLOSE = dt.datetime(2026, 10, 1, 18, 0, tzinfo=fetch.MARKET_TZ)   # cron 18:00 ET
OPEN = {"type": "open", "open": "09:30:00", "close": "16:00:00"}


class FakeTheta:
    """published[symbol] 里的日期才有 EOD；区间内一天都没有 → NoDataFoundError（同 NOT_FOUND）。"""

    def __init__(self, published: dict):
        self.published = {s: set(v) for s, v in published.items()}
        self.stock_calls: list[str] = []
        self.fail: dict[str, Exception] = {}

    def _days(self, symbol, start, end):
        return sorted(d for d in self.published.get(symbol, ()) if start <= d <= end)

    def stock_history_eod(self, symbol, start_date, end_date):
        self.stock_calls.append(symbol)
        if symbol in self.fail:
            raise self.fail[symbol]
        days = self._days(symbol, start_date, end_date)
        if not days:
            raise NoDataFoundError(f"No data found for: stock_history_eod({symbol})")
        return pd.DataFrame({"created": [pd.Timestamp(d) for d in days],
                             "close": [100.0] * len(days)})

    def option_list_expirations(self, symbol):
        return pd.DataFrame({"expiration": [pd.Timestamp(e) for e in EXPIRIES]})

    def option_history_eod(self, start_date, end_date, symbol, expiration):
        days = self._days(symbol, start_date, end_date)
        if not days:
            raise NoDataFoundError(f"No data found for: option_history_eod({symbol})")
        rows = [{"created": pd.Timestamp(d), "strike": 100.0, "right": r,
                 "bid": 1.0, "ask": 1.2, "volume": 10}
                for d in days for r in ("CALL", "PUT")]
        return pd.DataFrame(rows)


def _conn(symbols):
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.DDL)
    for s in symbols:
        conn.execute("INSERT INTO raw_chain (date, symbol, expiry, strike, right) "
                     "VALUES (?, ?, '2026-10-16', 100.0, 'C')", (PREV, s))
    return conn


def _latest(conn, s):
    return conn.execute("SELECT MAX(date) FROM raw_chain WHERE symbol=?", (s,)).fetchone()[0]


SOFR = pd.Series([0.04, 0.04], index=[dt.date(2026, 9, 30), REF])


@contextlib.contextmanager
def _env(theta, session=OPEN, session_exc=None, on_sleep=None):
    """假 ThetaData + 假日历；sleep 不真睡，记录参数（可挂回调模拟「等待期间数据源发布」）。"""
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if on_sleep and sec == eod_update.REFILL_WAIT_MIN * 60:
            on_sleep()

    cal = (mock.patch.object(fetch, "market_session_today", side_effect=session_exc)
           if session_exc else
           mock.patch.object(fetch, "market_session_today", return_value=session))
    with mock.patch.object(fetch, "_client", return_value=theta), \
         mock.patch.object(fetch, "market_today", return_value=REF), \
         cal, \
         mock.patch.object(eod_update, "compute_symbol") as comp, \
         mock.patch.object(eod_update, "postprocess_symbol"), \
         mock.patch.object(eod_update, "compute_state"), \
         mock.patch.object(eod_update.time, "sleep", side_effect=fake_sleep):
        yield sleeps, comp


def _run(conn, symbols, today=REF, now=AFTER_CLOSE):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        res = eod_update.run_incremental(conn, symbols, SOFR, today, now=now)
    return res, out.getvalue()


def _refill_sleeps(sleeps):
    return [s for s in sleeps if s == eod_update.REFILL_WAIT_MIN * 60]


# ---------- R1: 10-01 事故复现：SPY 有新日、QQQ NoData ----------
def test_partial_unpublished_goes_to_refill():
    print("\n[R1] SPY 已出 10-01、QQQ NoData → 判「尚未发布」并补拉")
    theta = FakeTheta({"SPY": {REF}, "QQQ": set()})
    conn = _conn(["SPY", "QQQ"])
    with _env(theta, on_sleep=lambda: theta.published["QQQ"].add(REF)) as (sleeps, comp):
        res, log = _run(conn, ["SPY", "QQQ"])
    qqq_lines = [ln for ln in log.splitlines() if "QQQ" in ln]
    check("日志写「尚未发布」", any("尚未发布" in ln for ln in qqq_lines), f"qqq={qqq_lines}")
    check("日志不再写「无新交易日」", "无新交易日" not in log, f"log={log!r}")
    check("参照日 = SPY 拉到的 10-01", res["ref"] == "2026-10-01", f"ref={res['ref']}")
    check("QQQ 进补拉列表（SPY 不进）", res["pending"] == ["QQQ"], f"pending={res['pending']}")
    check("补拉等了 1 轮", len(_refill_sleeps(sleeps)) == 1, f"sleeps={sleeps}")
    check("补拉后 QQQ 追到 10-01", _latest(conn, "QQQ") == "2026-10-01", _latest(conn, "QQQ"))
    check("无遗留未补齐", res["missing"] == [], f"missing={res['missing']}")
    check("SPY 不重复拉", theta.stock_calls.count("SPY") == 1, f"calls={theta.stock_calls}")
    # 首轮空表提前返回不计算；补上那轮才走计算 → 后处理 → 状态层
    check("补上后走了计算", [c.args[1] for c in comp.call_args_list] == ["SPY", "QQQ"],
          f"compute={[c.args[1] for c in comp.call_args_list]}")
    check("补拉成功后该推送", eod_update.should_push(res), f"res={res}")


# ---------- R2: 真休市 / 周末：全体都空 → 不补拉、不推送 ----------
def test_holiday_all_empty_no_refill():
    for kind in ("full_close", "weekend"):
        print(f"\n[R2] 全体都空 + 日历 {kind} → 不补拉、不推送")
        theta = FakeTheta({})
        conn = _conn(["SPY", "QQQ"])
        with _env(theta, session={"type": kind, "open": None, "close": None}) as (sleeps, _):
            res, log = _run(conn, ["SPY", "QQQ"])
        check(f"{kind}: 不进补拉", res["pending"] == [], f"pending={res['pending']}")
        check(f"{kind}: 没有等待", _refill_sleeps(sleeps) == [], f"sleeps={sleeps}")
        check(f"{kind}: 日志写「无新交易日」", "无新交易日" in log, f"log={log!r}")
        check(f"{kind}: 不写「尚未发布」", "尚未发布" not in log, f"log={log!r}")
        check(f"{kind}: 不推送", not eod_update.should_push(res), f"res={res}")


# ---------- R3: 全体都空 + 日历说开市且已收盘 → 数据源整体未发布 ----------
def test_all_empty_on_trading_day_refills_all():
    print("\n[R3] 全体都空 + 今天开市且已收盘（冬令时 17:00 EST / 数据源整体延迟）→ 全部补拉")
    theta = FakeTheta({})

    def publish_all():
        theta.published = {"SPY": {REF}, "QQQ": {REF}}

    conn = _conn(["SPY", "QQQ"])
    with _env(theta, session=OPEN, on_sleep=publish_all) as (sleeps, _):
        res, log = _run(conn, ["SPY", "QQQ"])
    check("参照日取今天", res["ref"] == "2026-10-01", f"ref={res['ref']}")
    check("全部进补拉", res["pending"] == ["SPY", "QQQ"], f"pending={res['pending']}")
    check("日志写「尚未发布」", "尚未发布" in log)
    check("补拉后全部追平", res["missing"] == [], f"missing={res['missing']}")
    check("补拉成功后该推送", eod_update.should_push(res))


# ---------- R4: 盘中手动跑（开市日、未收盘）→ 不补拉 ----------
def test_all_empty_before_close_no_refill():
    print("\n[R4] 全体都空 + 开市日但未收盘（盘中手动跑）→ 不补拉")
    theta = FakeTheta({})
    conn = _conn(["SPY"])
    with _env(theta, session=OPEN) as (sleeps, _):
        res, _log = _run(conn, ["SPY"], now=dt.datetime(2026, 10, 1, 11, 0, tzinfo=fetch.MARKET_TZ))
    check("不进补拉", res["pending"] == [], f"pending={res['pending']}")
    check("没有等待", _refill_sleeps(sleeps) == [], f"sleeps={sleeps}")


# ---------- R5: 日历查询失败 → 按原规格（不补拉），推送 fail-open ----------
def test_calendar_failure_fails_safe():
    print("\n[R5] 全体都空 + 日历查询失败 → 不补拉，但照常推送")
    theta = FakeTheta({})
    conn = _conn(["SPY", "QQQ"])
    with _env(theta, session_exc=RuntimeError("PERMISSION_DENIED")) as (sleeps, _):
        res, log = _run(conn, ["SPY", "QQQ"])
    check("不进补拉", res["pending"] == [], f"pending={res['pending']}")
    check("没有等待", _refill_sleeps(sleeps) == [])
    check("日志写「无新交易日」", "无新交易日" in log, f"log={log!r}")
    check("推送 fail-open", eod_update.should_push(res), f"res={res}")


# ---------- R6: 补拉次数有上限，补不上交给下次自愈 ----------
def test_refill_gives_up_after_max_tries():
    print(f"\n[R6] QQQ 一直不发布 → 补拉 {eod_update.REFILL_TRIES} 次后放弃")
    theta = FakeTheta({"SPY": {REF}, "QQQ": set()})
    conn = _conn(["SPY", "QQQ"])
    with _env(theta) as (sleeps, _):
        res, log = _run(conn, ["SPY", "QQQ"])
    check("等了上限次数", len(_refill_sleeps(sleeps)) == eod_update.REFILL_TRIES, f"sleeps={sleeps}")
    check("总窗口 ≤ 75 分钟（market-agent 07:40 SGT 读 RND）",
          eod_update.REFILL_TRIES * eod_update.REFILL_WAIT_MIN <= 75)
    check("QQQ 记为未补齐", res["missing"] == ["QQQ"], f"missing={res['missing']}")
    check("日志写「仍未发布」", "仍未发布" in log, f"log={log!r}")
    check("库内 QQQ 不动（下次 MAX(date)+1 自愈）", _latest(conn, "QQQ") == PREV)
    check("补不上仍推送（带陈旧告警）", eod_update.should_push(res))


# ---------- R7: 拉取异常（非空表）的标的不进补拉 ----------
def test_fetch_error_not_refilled():
    print("\n[R7] QQQ 拉取抛异常（非 NoData）→ 记失败，不进补拉（免得确定性错误把推送拖满 75 分钟）")
    theta = FakeTheta({"SPY": {REF}, "QQQ": {REF}})
    theta.fail["QQQ"] = RuntimeError("UNAVAILABLE")
    conn = _conn(["SPY", "QQQ"])
    with _env(theta) as (sleeps, _):
        res, log = _run(conn, ["SPY", "QQQ"])
    check("QQQ 不进补拉", res["pending"] == [], f"pending={res['pending']}")
    check("没有补拉等待", _refill_sleeps(sleeps) == [], f"sleeps={sleeps}")
    check("日志标出失败", "增量更新失败" in log, f"log={log!r}")


# ---------- R8: 周末跑但迟到的周四数据到了 → 有新数据就推 ----------
def test_weekend_with_late_data_still_pushes():
    print("\n[R8] 周六（日历 weekend）拿到迟到的 10-01 → 有新数据，照常推送")
    theta = FakeTheta({"SPY": {REF}, "QQQ": {REF}})
    conn = _conn(["SPY", "QQQ"])
    with _env(theta, session={"type": "weekend", "open": None, "close": None}) as (sleeps, _):
        res, _log = _run(conn, ["SPY", "QQQ"], today=dt.date(2026, 10, 3),
                         now=dt.datetime(2026, 10, 3, 18, 0, tzinfo=fetch.MARKET_TZ))
    check("不进补拉", res["pending"] == [], f"pending={res['pending']}")
    check("有新数据", res["got_new"], f"res={res}")
    check("照常推送", eod_update.should_push(res), f"res={res}")


# ---------- R9: main 编排：补拉结束后才推送；休市不推 ----------
class _NoClose:
    """main 推送前会 close 连接；测试要在之后继续查库。"""

    def __init__(self, c):
        self._c = c

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._c, name)


def _main(theta, conn, session, on_sleep=None, events=None):
    events = [] if events is None else events
    pushed = []

    def fake_push(symbols, **kw):
        events.append("push")
        pushed.append(kw)
        return ["ok"]

    def sleep_cb():
        events.append("refill-sleep")
        if on_sleep:
            on_sleep()

    import push_daily
    out = io.StringIO()
    with _env(theta, session=session, on_sleep=sleep_cb), \
         mock.patch.object(sys, "argv", ["eod_update.py"]), \
         mock.patch("rnd.holdings_sync.sync", return_value={"skipped": "test"}), \
         mock.patch("server.pool.effective_symbols", return_value=["SPY", "QQQ"]), \
         mock.patch.object(eod_update.db, "get_conn", return_value=_NoClose(conn)), \
         mock.patch.object(fetch, "fetch_sofr_series", return_value=SOFR), \
         mock.patch("rnd.journal.roll_repin_check", return_value=[]), \
         mock.patch.object(push_daily, "run", side_effect=fake_push), \
         mock.patch.object(eod_update, "_now_et", return_value=AFTER_CLOSE), \
         contextlib.redirect_stdout(out):
        eod_update.main()
    return events, pushed, out.getvalue()


def test_main_pushes_after_refill():
    print("\n[R9] main：落后日整条推送推迟到补拉结束；休市日不推")
    theta = FakeTheta({"SPY": {REF}, "QQQ": set()})
    events, pushed, _log = _main(theta, _conn(["SPY", "QQQ"]), OPEN,
                                 on_sleep=lambda: theta.published["QQQ"].add(REF))
    check("先补拉、后推送", events == ["refill-sleep", "push"], f"events={events}")
    check("推送带参照日（陈旧闸门按它判）",
          pushed and pushed[0].get("ref_day") == "2026-10-01", f"pushed={pushed}")

    theta = FakeTheta({})
    events, pushed, log = _main(theta, _conn(["SPY", "QQQ"]),
                                {"type": "full_close", "open": None, "close": None})
    check("休市日不推送", events == [], f"events={events}")
    check("日志说明休市不推", "休市" in log, f"log={log!r}")


for fn in (test_partial_unpublished_goes_to_refill, test_holiday_all_empty_no_refill,
           test_all_empty_on_trading_day_refills_all, test_all_empty_before_close_no_refill,
           test_calendar_failure_fails_safe, test_refill_gives_up_after_max_tries,
           test_fetch_error_not_refilled, test_weekend_with_late_data_still_pushes,
           test_main_pushes_after_refill):
    try:
        fn()
    except Exception as e:  # noqa: BLE001  一项崩不挡其它项
        print(f"  FAIL  {fn.__name__} 崩溃: {type(e).__name__}: {e}")
        _failed.append(fn.__name__)

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

"""回填/增量的 NoData 容错 + ThetaData 单会话串行化回归（2026-07-27 部署发现）。

纯 mock，不实拉网络。覆盖：
  B1 backfill 串行派发（一个进程处理所有新标的，不并发抢 ThetaData 会话）
  B2 NoDataFoundError 优雅处理（上市前块跳过 / 无新交易日返回空 / with_retry 不重试）
    .venv/bin/python tests/test_backfill_resilience.py
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import datetime as dt

import pandas as pd
from thetadata.errors import NoDataFoundError

from rnd import fetch
import backfill

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


# ---------- B2a: chunked 块级跳过上市前无数据 ----------
def test_chunked_skips_nodata():
    print("\n[B2a] stock_history_eod_chunked 块级容错")
    calls = []

    def fake_eod(symbol, start_date, end_date):
        calls.append((start_date, end_date))
        # 前两块（上市前）无数据，第三块起有数据
        if start_date < dt.date(2024, 7, 1):
            raise NoDataFoundError(f"No data: {symbol} {start_date}")
        return pd.DataFrame({"created": [pd.Timestamp(start_date)], "close": [100.0]})

    with mock.patch.object(fetch, "_client") as mc:
        mc.return_value.stock_history_eod.side_effect = fake_eod
        df = fetch.stock_history_eod_chunked("SNDK", dt.date(2023, 7, 25), dt.date(2026, 7, 24))
    check("上市前块被跳过、上市后数据保留", not df.empty and len(df) >= 1, f"rows={len(df)}")
    check("确实尝试了多个时间块", len(calls) >= 3, f"chunks={len(calls)}")

    # 全窗口无数据 → 返回空 DataFrame（不抛），供 update_symbol 的 if empty 接住
    with mock.patch.object(fetch, "_client") as mc:
        mc.return_value.stock_history_eod.side_effect = NoDataFoundError("none")
        df = fetch.stock_history_eod_chunked("X", dt.date(2026, 7, 25), dt.date(2026, 7, 27))
    check("全窗口无数据返回空 DataFrame（不冒泡崩）", df.empty, f"empty={df.empty}")


# ---------- B2b: with_retry 对 NoData 不重试 ----------
def test_with_retry_no_retry_on_nodata():
    print("\n[B2b] with_retry 对 NoDataFoundError 不重试")
    n = {"calls": 0}

    def raises_nodata():
        n["calls"] += 1
        raise NoDataFoundError("gone")

    try:
        backfill.with_retry(raises_nodata, tries=4, base=0, label="t")
    except NoDataFoundError:
        pass
    check("NoData 立即抛、不重试（只调 1 次）", n["calls"] == 1, f"calls={n['calls']}")

    # 普通异常仍重试
    m = {"calls": 0}

    def raises_transient():
        m["calls"] += 1
        raise RuntimeError("transient")

    try:
        backfill.with_retry(raises_transient, tries=3, base=0, label="t")
    except RuntimeError:
        pass
    check("普通异常仍重试到上限", m["calls"] == 3, f"calls={m['calls']}")


# ---------- B2c: backfill main 单标的失败隔离 ----------
def test_backfill_per_symbol_isolation():
    print("\n[B2c] backfill main 单标的失败不中断其它")
    processed = []

    def fake_ingest(conn, symbol, start, end, sofr):
        if symbol == "BADSYM":
            raise RuntimeError("链数据异常")
        processed.append(symbol)

    argv = ["backfill.py", "--symbols", "GOOD1,BADSYM,GOOD2", "--years", "3", "--skip-fetch"]
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.object(backfill, "ingest_symbol", fake_ingest), \
         mock.patch.object(backfill.fetch, "latest_trading_day",
                           return_value=(dt.date(2026, 7, 24), 100.0)), \
         mock.patch.object(backfill.fetch, "fetch_sofr_series",
                           return_value=pd.Series([0.05], index=[dt.date(2026, 7, 24)])), \
         mock.patch.object(backfill.db, "get_conn"), \
         mock.patch.object(backfill, "compute_symbol"), \
         mock.patch.object(backfill, "postprocess_symbol"), \
         mock.patch("rnd.state.compute_state"):
        # BADSYM 走 --skip-fetch 不经 ingest，改让 compute_symbol 对它抛
        def fake_compute(conn, symbol, recompute):
            if symbol == "BADSYM":
                raise RuntimeError("计算炸")
            processed.append(symbol)
        backfill.compute_symbol.side_effect = fake_compute
        backfill.db.get_conn.return_value.execute.return_value.fetchone.return_value = (0, 0)
        backfill.main()
    check("GOOD1/GOOD2 都处理了（BADSYM 崩不中断）",
          "GOOD1" in processed and "GOOD2" in processed, f"processed={processed}")
    check("BADSYM 未混入成功列表", "BADSYM" not in processed, f"processed={processed}")


# ---------- B1: eod_update 串行派发一个进程 ----------
def test_backfill_dispatch_serial():
    print("\n[B1] eod_update 一次派发一个 backfill 进程（串行，非并发）")
    import eod_update
    popen_calls = []

    class FakePopen:
        def __init__(self, args, **kw):
            popen_calls.append(args)

    with mock.patch("subprocess.Popen", FakePopen):
        eod_update._spawn_backfill(["GOOGL", "INTC", "MU", "SNDK"])
    check("只 spawn 1 个进程（而非 4 个并发）", len(popen_calls) == 1, f"进程数={len(popen_calls)}")
    args = popen_calls[0] if popen_calls else []
    joined = ",".join(["GOOGL", "INTC", "MU", "SNDK"])
    check("该进程 --symbols 含全部 4 个标的", joined in args, f"args={args}")


for fn in (test_chunked_skips_nodata, test_with_retry_no_retry_on_nodata,
           test_backfill_per_symbol_isolation, test_backfill_dispatch_serial):
    fn()

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

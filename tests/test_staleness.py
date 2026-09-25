"""推送层陈旧闸门（2026-09-01 事故防复发）。

事故形态：拉取全挂 → 库冻在 08-28，但 push_daily 照常把同一份 08-28 异动 +
市场展望每天发一遍，看起来一切正常，静默两天。闸门口径：以数据源
（ThetaData）已出 EOD 的最近交易日为基准，库内落后即告警；全面落后就不再
推异动/展望（不把旧数据当新的发，也不白烧一次 LLM）。

纯 mock，不实拉网络、不读真库：
    .venv/bin/python tests/test_staleness.py
"""
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import datetime as dt

from server import alerts
import push_daily

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


def _fake_db(latest: dict):
    """把 queries.conn/latest_date 换成内存映射。"""
    return (mock.patch.object(alerts.q, "conn", return_value=mock.MagicMock()),
            mock.patch.object(alerts.q, "latest_date",
                              side_effect=lambda c, s: latest.get(s)))


# ---------- 1. stale_symbols 口径 ----------
def test_stale_symbols():
    print("\n[1] stale_symbols 只报落后数据源的标的")
    latest = {"SPY": "2026-08-28", "QQQ": "2026-09-01", "NVDA": None}
    c1, c2 = _fake_db(latest)
    with c1, c2:
        stale = alerts.stale_symbols(["SPY", "QQQ", "NVDA"], "2026-09-01")
    names = [s for s, _ in stale]
    check("落后的进列表", "SPY" in names, f"stale={stale}")
    check("追平的不进列表", "QQQ" not in names, f"stale={stale}")
    check("完全无数据的也算陈旧", "NVDA" in names, f"stale={stale}")
    check("带出库内最新日", dict(stale).get("SPY") == "2026-08-28", f"stale={stale}")

    # 周末：数据源最新仍是周五，库内也是周五 → 不算陈旧（无需自维护交易日历）
    c1, c2 = _fake_db({"SPY": "2026-08-28", "QQQ": "2026-08-28"})
    with c1, c2:
        weekend = alerts.stale_symbols(["SPY", "QQQ"], "2026-08-28")
    check("周末与数据源持平不误报", weekend == [], f"stale={weekend}")


# ---------- 2. 告警文本 ----------
def test_format_staleness():
    print("\n[2] 告警文本含关键事实")
    text = alerts.format_staleness([("SPY", "2026-08-28"), ("QQQ", None)],
                                   "2026-09-01", 2)
    for token in ("2026-09-01", "2026-08-28", "SPY", "QQQ"):
        check(f"含 {token}", token in text)
    check("标明是陈旧告警而非正常报告", "陈旧" in text and "异动" not in text.split("\n")[0],
          repr(text.split("\n")[0]))


# ---------- 3. push_daily：全面陈旧 → 只推告警，不推异动/展望 ----------
def test_push_all_stale():
    print("\n[3] 全面陈旧：不推异动、不烧 LLM")
    sends, generated = [], []
    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x"}), \
         mock.patch.object(push_daily.fetch, "latest_trading_day",
                           return_value=(dt.date(2026, 9, 1), 640.0)), \
         mock.patch.object(push_daily.alerts, "stale_symbols",
                           return_value=[("SPY", "2026-08-28"), ("QQQ", "2026-08-28")]), \
         mock.patch.object(push_daily.alerts, "todays_anomalies", return_value=[]) as anom, \
         mock.patch.object(push_daily.assistant, "generate",
                           side_effect=lambda *a: generated.append(a) or "outlook"), \
         mock.patch.object(push_daily.telegram, "send", side_effect=sends.append):
        sent = push_daily.run(["SPY", "QQQ"])
    check("推了 1 条告警", len(sends) == 1, f"sends={len(sends)}")
    check("告警文本是陈旧告警", sends and "陈旧" in sends[0], repr(sends[0][:30]) if sends else "")
    check("没算异动", anom.call_count == 0, f"calls={anom.call_count}")
    check("没调 LLM 展望", generated == [], f"generated={generated}")
    check("返回值标出陈旧", any("陈旧" in s for s in sent), f"sent={sent}")


# ---------- 4. push_daily：部分陈旧 → 告警 + 正常推送照旧 ----------
def test_push_partial_stale():
    print("\n[4] 部分陈旧：告警 + 其余标的照常推")
    sends, generated = [], []
    items = [{"symbol": "QQQ", "date": "2026-09-01", "kind": "extreme", "text": "QQQ x 升至 P95"}]
    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x"}), \
         mock.patch.object(push_daily.fetch, "latest_trading_day",
                           return_value=(dt.date(2026, 9, 1), 640.0)), \
         mock.patch.object(push_daily.alerts, "stale_symbols",
                           return_value=[("SPY", "2026-08-28")]), \
         mock.patch.object(push_daily.alerts, "todays_anomalies", return_value=items), \
         mock.patch.object(push_daily.assistant, "build_outlook_messages",
                           return_value=({"system": "s", "user": "u"}, "2026-09-01")), \
         mock.patch.object(push_daily.assistant, "generate",
                           side_effect=lambda *a: generated.append(a) or "outlook"), \
         mock.patch.object(push_daily.telegram, "send", side_effect=sends.append):
        push_daily.run(["SPY", "QQQ"])
    check("推了告警 + 异动 + 展望 共 3 条", len(sends) == 3, f"sends={len(sends)}")
    check("第一条是陈旧告警", sends and "陈旧" in sends[0])
    check("仍调了 LLM 展望", len(generated) == 1, f"generated={len(generated)}")


# ---------- 5. push_daily：不陈旧 → 一切照旧 ----------
def test_push_fresh():
    print("\n[5] 数据新鲜：不加任何告警")
    sends = []
    items = [{"symbol": "SPY", "date": "2026-09-01", "kind": "extreme", "text": "SPY x 升至 P95"}]
    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x"}), \
         mock.patch.object(push_daily.fetch, "latest_trading_day",
                           return_value=(dt.date(2026, 9, 1), 640.0)), \
         mock.patch.object(push_daily.alerts, "stale_symbols", return_value=[]), \
         mock.patch.object(push_daily.alerts, "todays_anomalies", return_value=items), \
         mock.patch.object(push_daily.assistant, "build_outlook_messages",
                           return_value=({"system": "s", "user": "u"}, "2026-09-01")), \
         mock.patch.object(push_daily.assistant, "generate", return_value="outlook"), \
         mock.patch.object(push_daily.telegram, "send", side_effect=sends.append):
        push_daily.run(["SPY"])
    check("只有异动 + 展望 2 条", len(sends) == 2, f"sends={len(sends)}")
    check("无陈旧字样", all("陈旧" not in s for s in sends))


# ---------- 6. 闸门自身失败不阻断推送（fail-open）----------
def test_guard_fails_open():
    print("\n[6] 拿不到数据源交易日：闸门跳过，不阻断推送")
    sends = []
    items = [{"symbol": "SPY", "date": "2026-09-01", "kind": "extreme", "text": "SPY x 升至 P95"}]
    with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x"}), \
         mock.patch.object(push_daily.fetch, "latest_trading_day",
                           side_effect=RuntimeError("gRPC down")), \
         mock.patch.object(push_daily.alerts, "todays_anomalies", return_value=items), \
         mock.patch.object(push_daily.assistant, "build_outlook_messages",
                           return_value=({"system": "s", "user": "u"}, "2026-09-01")), \
         mock.patch.object(push_daily.assistant, "generate", return_value="outlook"), \
         mock.patch.object(push_daily.telegram, "send", side_effect=sends.append):
        push_daily.run(["SPY"])
    check("异动 + 展望 照常推", len(sends) == 2, f"sends={len(sends)}")


for fn in (test_stale_symbols, test_format_staleness, test_push_all_stale,
           test_push_partial_stale, test_push_fresh, test_guard_fails_open):
    fn()

print()
if _failed:
    print(f"❌ {len(_failed)} 项失败: {_failed}")
    sys.exit(1)
print("全部通过。")

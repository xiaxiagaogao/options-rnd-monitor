"""第 0 步：ThetaData 免费档端点权限实测。结论回填 rnd-dashboard-spec.md §1。

用法：cd 项目根目录后
    grpc_proxy=http://127.0.0.1:7897 .venv/bin/python scripts/step0_thetadata_probe.py
"""
import datetime as dt
import os
import time

os.environ.setdefault("grpc_proxy", "http://127.0.0.1:7897")

from thetadata import ThetaClient

LAST_TRADING_DAY = dt.date(2026, 7, 10)   # 上一交易日（周五）
AUG_MONTHLY = dt.date(2026, 8, 21)        # 最近的 7-60 DTE 月度到期
ONE_YEAR_AGO = dt.date(2025, 7, 11)       # 免费档宣称的历史边界附近
TWO_YEARS_AGO = dt.date(2024, 7, 12)      # 边界之外，预期被拒
EXP_FOR_2025 = dt.date(2025, 8, 15)       # 2025-07 时点的近月月度
EXP_FOR_2024 = dt.date(2024, 8, 16)       # 2024-07 时点的近月月度


def probe(name, fn):
    t0 = time.time()
    try:
        df = fn()
        ms = (time.time() - t0) * 1000
        print(f"\n=== {name} — OK ({ms:.0f}ms, {len(df)} rows) ===")
        print("columns:", list(df.columns))
        print(df.head(3).to_string())
        return df
    except Exception as e:
        ms = (time.time() - t0) * 1000
        msg = str(e)
        # gRPC 异常冗长，抽 status/details 就够
        for line in msg.splitlines():
            if "status" in line.lower() or "details" in line.lower():
                msg = line.strip()
                break
        print(f"\n=== {name} — FAIL ({ms:.0f}ms) ===\n{type(e).__name__}: {msg[:300]}")
        return None


client = ThetaClient(dotenv_path=".env", dataframe_type="pandas")

# 1. EOD 期权报告：字段与 close bid/ask 口径
probe("1. option_history_eod SPY 2026-08-21 @ 上一交易日", lambda: client.option_history_eod(
    start_date=LAST_TRADING_DAY, end_date=LAST_TRADING_DAY,
    symbol="SPY", expiration=AUG_MONTHLY))

# 2. OI 端点是否开放
probe("2. option_history_open_interest 同链", lambda: client.option_history_open_interest(
    symbol="SPY", expiration=AUG_MONTHLY, date=LAST_TRADING_DAY))

# 3. 历史深度：1 年边界内
probe("3. EOD 一年前 (2025-07-11)", lambda: client.option_history_eod(
    start_date=ONE_YEAR_AGO, end_date=ONE_YEAR_AGO,
    symbol="SPY", expiration=EXP_FOR_2025))

# 4. 历史深度：边界外（预期拒绝，验证边界在哪）
probe("4. EOD 两年前 (2024-07-12, 预期越界)", lambda: client.option_history_eod(
    start_date=TWO_YEARS_AGO, end_date=TWO_YEARS_AGO,
    symbol="SPY", expiration=EXP_FOR_2024))

# 5. 利率端点（若含 SOFR 则免接 FRED）
probe("5. interest_rate_history_eod SOFR", lambda: client.interest_rate_history_eod(
    symbol="SOFR", start_date=dt.date(2026, 7, 1), end_date=LAST_TRADING_DAY))

# 6. 标的收盘
probe("6. stock_history_eod SPY", lambda: client.stock_history_eod(
    symbol="SPY", start_date=dt.date(2026, 7, 6), end_date=LAST_TRADING_DAY))

# 7. 限速行为：连发 25 个轻请求（超过宣称的 20 req/min）观察是否 429/限流
print("\n=== 7. 限速实测：25 连发 option_list_strikes ===")
t0 = time.time()
ok, fail = 0, 0
for i in range(25):
    try:
        client.option_list_strikes(symbol="SPY", expiration=AUG_MONTHLY)
        ok += 1
    except Exception as e:
        fail += 1
        print(f"  第 {i+1} 发失败: {type(e).__name__}: {str(e)[:150]}")
        break
print(f"  {ok} OK / {fail} FAIL, 用时 {time.time()-t0:.1f}s")

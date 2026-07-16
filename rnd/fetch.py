"""ThetaData 数据获取层。EOD 报告口径（spec §1）。"""
import datetime as dt
from functools import lru_cache

import pandas as pd

from . import config  # noqa: F401  # 先加载 .env 与 grpc_proxy


@lru_cache(maxsize=1)
def _client():
    from thetadata import ThetaClient
    return ThetaClient(
        dotenv_path=str(config.PROJECT_ROOT / ".env"),
        dataframe_type="pandas",
    )


def third_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def is_monthly(e: dt.date, listed: set) -> bool:
    """月度 = 第三个周五本尊；本尊不在挂牌列表（假日休市顺延）时才接受 ±1 天。
    直接用 ±1 容差会把紧邻的周四 weekly 误判成月度（2026-07-13 实测踩坑）。"""
    tf = third_friday(e.year, e.month)
    if e == tf:
        return True
    return abs((e - tf).days) <= 1 and tf not in listed


def monthly_expirations(symbol: str, asof: dt.date, dte_min=7, dte_max=60, count=2):
    """最近 count 个月度到期（DTE 限制内），从真实到期日列表中筛。"""
    exps = _client().option_list_expirations(symbol=symbol)
    dates = sorted(pd.to_datetime(exps["expiration"]).dt.date)
    listed = set(dates)
    picks = []
    for e in dates:
        dte = (e - asof).days
        if dte_min <= dte <= dte_max and is_monthly(e, listed):
            picks.append(e)
        if len(picks) >= count:
            break
    return picks


def fetch_chain_eod(symbol: str, expiry: dt.date, date: dt.date) -> pd.DataFrame:
    """一次请求拉整链 EOD 报告（close bid/ask、volume）。"""
    return _client().option_history_eod(
        start_date=date, end_date=date, symbol=symbol, expiration=expiry
    )


def fetch_underlying_close(symbol: str, date: dt.date) -> float:
    df = _client().stock_history_eod(symbol=symbol, start_date=date, end_date=date)
    return float(df["close"].iloc[-1])


def stock_history_eod_chunked(symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """股票 EOD 范围请求实测上限 365 天，超出的窗口分块拼接。"""
    frames = []
    s = start
    while s <= end:
        e = min(s + dt.timedelta(days=350), end)
        frames.append(_client().stock_history_eod(symbol=symbol, start_date=s, end_date=e))
        s = e + dt.timedelta(days=1)
    return pd.concat(frames, ignore_index=True)


def latest_trading_day(symbol: str = "SPY") -> tuple[dt.date, float]:
    """最近一个已出 EOD 报告的交易日及其收盘价。"""
    today = dt.date.today()
    df = _client().stock_history_eod(
        symbol=symbol, start_date=today - dt.timedelta(days=10), end_date=today
    )
    last = df.iloc[-1]
    return pd.to_datetime(last["created"]).date(), float(last["close"])


def fetch_sofr(date: dt.date) -> float:
    """SOFR 年化利率（小数）。EOD 当日若未发布则取最近一期。"""
    df = _client().interest_rate_history_eod(
        symbol="SOFR", start_date=date - dt.timedelta(days=7), end_date=date
    )
    return float(df["rate"].iloc[-1]) / 100.0


THETA_RATES_FLOOR = dt.date(2024, 1, 1)   # 利率端点实测历史边界（spec §1）


def fetch_sofr_series(start: dt.date, end: dt.date) -> pd.Series:
    """日频 SOFR 序列（小数）。≥2024-01 走 ThetaData，更早的段走 FRED CSV 备胎。"""
    parts = []
    if start < THETA_RATES_FLOOR:
        import io
        import httpx
        url = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
               f"?id=SOFR&cosd={start}&coed={min(end, THETA_RATES_FLOOR)}")
        resp = httpx.get(url, timeout=30)
        resp.raise_for_status()
        fred = pd.read_csv(io.StringIO(resp.text), na_values=".")
        fred.columns = ["date", "rate"]
        fred["date"] = pd.to_datetime(fred["date"]).dt.date
        parts.append(fred.dropna())
    if end >= THETA_RATES_FLOOR:
        td = _client().interest_rate_history_eod(
            symbol="SOFR", start_date=max(start, THETA_RATES_FLOOR), end_date=end)
        td = td.rename(columns={"created": "date"})
        td["date"] = pd.to_datetime(td["date"]).dt.date
        parts.append(td[["date", "rate"]])
    s = (pd.concat(parts).drop_duplicates("date").set_index("date")["rate"]
         .sort_index() / 100.0)
    return s

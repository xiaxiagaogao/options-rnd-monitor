"""每日 TG 推送（research-assistant-framework §2.3）：期权异动 + 综合摘要。

挂在 eod_update 之后自动跑，或单独手动/测试：
    .venv/bin/python scripts/push_daily.py              # 异动 + 摘要
    .venv/bin/python scripts/push_daily.py --alerts     # 只推异动（快，不调 LLM）
    .venv/bin/python scripts/push_daily.py --dry         # 只打印不发送

未配 TELEGRAM_BOT_TOKEN → 整体跳过（不白跑 1–3 分钟的 Fable 摘要）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import alerts, assistant, queries
from rnd import fetch, telegram


def run(symbols: list[str] | None = None, *, do_digest: bool = True, dry: bool = False) -> list[str]:
    if not dry and not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise telegram.TelegramNotConfigured(
            "未配 TELEGRAM_BOT_TOKEN，跳过推送（.env 加 token 后生效）")
    syms = symbols or queries.get_symbols()
    sent: list[str] = []

    # 0. 陈旧闸门：库内数据日落后数据源 → 先推告警；全面陈旧则不再推异动/展望。
    #    2026-09-01 事故根因之二——增量拉取全挂而推送毫不知情，把同一份 08-28
    #    报告当新的连发两天。闸门自身失败一律 fail-open（宁可多推也别静默）。
    stale: list[tuple[str, str | None]] = []
    vendor_latest = None
    try:
        vendor_latest = fetch.latest_trading_day("SPY")[0].isoformat()
        stale = alerts.stale_symbols(syms, vendor_latest)
    except Exception as e:  # noqa: BLE001
        print(f"--- 陈旧闸门 --- 跳过（拿不到数据源交易日: {type(e).__name__}: {e}）")
    if stale:
        stale_text = alerts.format_staleness(stale, vendor_latest, len(syms))
        print("--- 陈旧告警 ---\n" + stale_text)
        if not dry:
            telegram.send(stale_text)
        sent.append(f"陈旧告警 {len(stale)}/{len(syms)}")
        if len(stale) >= len(syms):
            return sent

    # 1. 期权异动（确定性，快）
    items = alerts.todays_anomalies(syms)
    alert_text = alerts.format_alerts(items)
    if alert_text:
        print("--- 异动 ---\n" + alert_text)
        if not dry:
            telegram.send(alert_text)
        sent.append(f"异动 {len(items)} 项")
    else:
        print("--- 异动 --- 无")

    # 2. 全量市场展望（C++ outlook，Fable max，1–3 分钟）——每日汇报复用此模式
    if do_digest:
        msg, asof = assistant.build_outlook_messages(syms)
        # 模型按模板自带标题+口径日期，不再叠加脚本头，避免双标题。
        outlook = assistant.generate(msg["system"], msg["user"])
        print("--- 市场展望 ---\n" + outlook)
        if not dry:
            telegram.send(outlook)
        sent.append("市场展望")

    return sent


def main():
    dry = "--dry" in sys.argv
    only_alerts = "--alerts" in sys.argv
    try:
        sent = run(do_digest=not only_alerts, dry=dry)
        print("\n推送完成:", sent or "无内容")
    except telegram.TelegramNotConfigured as e:
        print(f"跳过推送: {e}")


if __name__ == "__main__":
    main()

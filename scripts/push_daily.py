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
from rnd import telegram


def run(symbols: list[str] | None = None, *, do_digest: bool = True, dry: bool = False) -> list[str]:
    if not dry and not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise telegram.TelegramNotConfigured(
            "未配 TELEGRAM_BOT_TOKEN，跳过推送（.env 加 token 后生效）")
    syms = symbols or queries.get_symbols()
    sent: list[str] = []

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

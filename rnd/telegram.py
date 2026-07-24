"""Telegram 推送（research-assistant-framework §2.3）。

send() 走 Bot API，token/chat_id 读 .env（TELEGRAM_BOT_TOKEN 密钥、TELEGRAM_CHAT_ID 可选）。
纯文本发送（不用 Markdown parse_mode——报告/摘要里的表格/符号在 TG Markdown 会翻车，
纯文本最稳），单条 4096 上限按行分片。
"""
import os

import httpx

from . import config  # 触发 .env 加载

TG_LIMIT = 3900          # TG 单条 4096，留余量
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # 必配于 .env（无内置默认；见 .env.example）


class TelegramNotConfigured(RuntimeError):
    """未配 TELEGRAM_BOT_TOKEN。调用方据此静默跳过或提示，而非崩。"""


def _split(text: str, limit: int = TG_LIMIT) -> list[str]:
    """按行分片，每片 ≤ limit；单行超长则硬切。不丢字符。"""
    out, buf = [], ""
    for line in text.split("\n"):
        while len(line) > limit:          # 超长单行硬切
            if buf:
                out.append(buf); buf = ""
            out.append(line[:limit]); line = line[limit:]
        add = (buf + "\n" + line) if buf else line
        if len(add) > limit:
            out.append(buf); buf = line
        else:
            buf = add
    if buf:
        out.append(buf)
    return out


def send(text: str, chat_id: str | None = None) -> int:
    """推送到 TG，长文自动分片。返回发出的条数。未配 token → TelegramNotConfigured。"""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramNotConfigured(
            "未配置 TELEGRAM_BOT_TOKEN：请在 .env 加 TELEGRAM_BOT_TOKEN=<你的 bot token>")
    chat_id = chat_id or DEFAULT_CHAT_ID
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split(text)
    for chunk in chunks:
        r = httpx.post(url, data={"chat_id": chat_id, "text": chunk,
                                  "disable_web_page_preview": "true"}, timeout=30)
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):  # TG 常 HTTP 200 但 ok:false（chat 不存在/被封等）
            raise RuntimeError(f"Telegram 拒绝：{body.get('description', body)}")
    return len(chunks)

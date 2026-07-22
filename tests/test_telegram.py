"""Telegram 推送模块验证：未配置 token 须显式抛错；长文分片正确。

用法：.venv/bin/python tests/test_telegram.py
不触网——只验证配置契约与分片逻辑。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import telegram

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# --- 1. 专用异常类型 ---
check("有 TelegramNotConfigured", isinstance(getattr(telegram, "TelegramNotConfigured", None), type))

# --- 2. 未配 token → 抛 TelegramNotConfigured ---
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
raised = None
try:
    telegram.send("hi")
except Exception as e:  # noqa: BLE001
    raised = e
check("未配 token 抛 TelegramNotConfigured",
      isinstance(raised, getattr(telegram, "TelegramNotConfigured", ())),
      f"got={type(raised).__name__ if raised else None}")

# --- 3. 分片：长文按行切、每片 ≤ 上限、拼回等价（去掉分片补的换行）---
lines = "\n".join(f"第 {i} 行内容占位占位占位占位占位占位" for i in range(400))
chunks = telegram._split(lines, 1000)
check("分片数 >1", len(chunks) > 1, f"n={len(chunks)}")
check("每片不超上限", all(len(c) <= 1000 for c in chunks), f"maxlen={max(map(len,chunks))}")
check("分片无丢行", sum(c.count("第 ") for c in chunks) == 400)

# --- 4. 单行超长也能硬切不丢 ---
big = "x" * 2500
bc = telegram._split(big, 1000)
check("超长单行硬切", all(len(c) <= 1000 for c in bc) and "".join(bc) == big, f"n={len(bc)}")


if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

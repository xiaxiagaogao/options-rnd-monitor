"""研究助手 LLM 接缝（generate）的契约验证：未配置时须显式抛错、不静默。

用法：.venv/bin/python tests/test_assistant_generate.py
本测试不触网——只验证"未配置"这条自愈契约（用户后续填 .env 接入真实中转）。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import assistant

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# 强制未配置态：清掉可能存在的密钥
os.environ.pop("ASSISTANT_API_KEY", None)

# --- 1. 存在专用异常类型 ---
check("有 AssistantNotConfigured 异常类",
      isinstance(getattr(assistant, "AssistantNotConfigured", None), type))

# --- 2. 未配置密钥 → 抛 AssistantNotConfigured（不返回、不静默、不抛别的）---
raised = None
try:
    assistant.generate("system prompt", "user question")
except Exception as e:  # noqa: BLE001
    raised = e
check("未配置密钥时抛 AssistantNotConfigured",
      isinstance(raised, getattr(assistant, "AssistantNotConfigured", ())),
      f"got={type(raised).__name__ if raised else None}")

# --- 3. 错误信息可指导接入（提到 .env / 密钥 / 配置）---
msg = str(raised) if raised else ""
check("错误信息含接入指引", any(k in msg for k in ("ASSISTANT_API_KEY", ".env", "配置", "未配置")),
      f"msg={msg!r}")


if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

"""研究助手提示层验证（模型无关：系统提示纪律 + build_messages 组装）。

用法：.venv/bin/python tests/test_assistant_prompt.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import assistant

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


ctx = assistant.build_context("NVDA")
question = "现在偏度处在什么水平？"
msg = assistant.build_messages(ctx, question)

# --- 1. 结构：system + user 两段字符串 ---
check("build_messages 返回 dict", isinstance(msg, dict), f"type={type(msg).__name__}")
check("含 system 字符串", isinstance(msg.get("system"), str) and len(msg.get("system", "")) > 0)
check("含 user 字符串", isinstance(msg.get("user"), str) and len(msg.get("user", "")) > 0)

sysp = msg.get("system", "")
user = msg.get("user", "")

# --- 2. 系统提示编码硬纪律（framework §1.1 / §5）---
# 只锚关键词，不锁死措辞——refactor 不得意外丢纪律。
for kw, why in [
    ("风险中性", "RN≠真实概率 的口径声明"),
    ("投资建议", "只读·数据描述非建议"),
    ("收盘", "分位线是收盘确认失效线、非盘中止损"),
    ("样本", "样本<60 不报假精确分位"),
    ("闸门", "闸门是质量验收、非交易信号"),
    ("外推", "外推区/逐分位可信度披露"),
]:
    check(f"系统提示含纪律锚：{why}", kw in sysp, f"缺关键词『{kw}』")

# --- 3. 只准用装配数据、不得自算（framework §3.1）---
check("系统提示申明不得自算/invent 数值",
      ("不" in sysp and ("计算" in sysp or "自算" in sysp)) or "只" in sysp)

# --- 4. user 段：嵌入问题 + 上下文（symbol/asof 可见）---
check("user 段包含用户问题原文", question in user, "问题未嵌入")
check("user 段包含标的 NVDA", "NVDA" in user)
check("user 段包含数据日 asof", ctx["meta"]["asof"] in user)

# --- 5. 纯函数：不触网、不写库（无副作用签名）---
msg2 = assistant.build_messages(ctx, question)
check("build_messages 确定性（同输入同输出）", msg == msg2)


if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

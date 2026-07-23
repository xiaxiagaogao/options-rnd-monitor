"""全量市场展望装配（build_outlook_messages）验证：结构 + C++ 红线锚点。

用法：.venv/bin/python tests/test_outlook.py
只验组装（不触网/不调 LLM）。调性由原型人工锁定，此处只保证约束不被 refactor 丢。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import assistant, queries

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


syms = queries.get_symbols()
msg, asof = assistant.build_outlook_messages(syms)

# --- 1. 结构：(messages, asof)，system 复用九纪律，user 非空 ---
check("返回 (dict, asof)", isinstance(msg, dict) and isinstance(asof, (str, type(None))))
check("system == SYSTEM_PROMPT（复用九纪律）", msg.get("system") == assistant.SYSTEM_PROMPT)
check("user 非空字符串", isinstance(msg.get("user"), str) and len(msg.get("user", "")) > 0)

user = msg.get("user", "")

# --- 2. 全量：每个自选标的都进了包 ---
check("每个标的都嵌入", all(s in user for s in syms), f"syms={syms}")
check("含数据日 asof", (asof or "") in user and asof)

# --- 3. C++ 调性锚点（refactor 不得丢）---
for kw, why in [
    ("市场展望", "定位为 outlook 非数据复述"),
    ("总纲", "开篇板块基调"),
    ("净判断", "决断度"),
    ("情景触发", "若…则… 观察项"),
    ("顺风", "对已有论点的顺逆风判定（C++）"),
]:
    check(f"含调性锚：{why}", kw in user, f"缺『{kw}』")

# --- 4. 红线锚点（不下单）必须在指令里 ---
check("红线：禁止动作类词", all(k in user for k in ("禁止", "不下单")) or "那个键" in user,
      "红线约束缺失")
check("红线：仍禁方向预测", "涨跌" in user or "方向" in user)

# --- 5. 确定性 ---
msg2, _ = assistant.build_outlook_messages(syms)
check("同输入同输出", msg == msg2)


if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

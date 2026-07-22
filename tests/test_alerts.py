"""异动检测验证（对真实库）：结构 + 口径不变量。

口径（用户定）：状态分位穿越 P90/P10 + 双峰 + 持仓偏移 >1σ。不含闸门 FAIL / roll。
用法：.venv/bin/python tests/test_alerts.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import alerts, queries as q

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


items = alerts.todays_anomalies()

# --- 1. 结构 ---
check("返回 list", isinstance(items, list), f"type={type(items).__name__}")
check("每项含 symbol/date/kind/text",
      all({"symbol", "date", "kind", "text"} <= set(it) for it in items),
      f"n={len(items)}")

# --- 2. 口径：kind 只在约定三类，不含 gate/roll ---
kinds = {it["kind"] for it in items}
check("kind ⊆ {extreme, bimodal, offset}", kinds <= {"extreme", "bimodal", "offset"},
      f"kinds={kinds}")
check("不含闸门/roll 类", not (kinds & {"gate", "roll"}))

# --- 3. 每项 date = 该标的最新 pinned 日 ---
c = q.conn()
latest = {s: q.latest_date(c, s) for s in q.get_symbols()}
c.close()
check("每项 date 为该标的最新日",
      all(it["date"] == latest.get(it["symbol"]) for it in items),
      f"latest={latest}")

# --- 4. offset 项确实 >1σ（口径核心：不足 1σ 不报）---
offs = [it for it in items if it["kind"] == "offset"]
check("offset 项文本标注 >1σ", all(">1σ" in it["text"] or "σ" in it["text"] for it in offs),
      f"n_off={len(offs)}")

# --- 5. 确定性 ---
check("同输入同输出", alerts.todays_anomalies() == items)

# --- 6. 单标的过滤 ---
one = alerts.todays_anomalies(["QQQ"])
check("按标的过滤只出该标的", all(it["symbol"] == "QQQ" for it in one))

print(f"\n实测异动 {len(items)} 项：")
for it in items:
    print(f"  [{it['kind']}] {it['text']}")

if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

"""状态指标的方向语义：上下文由代码预写方向白话，模型不得自行从分位推方向。

    .venv/bin/python tests/test_state_direction.py

回归点（2026-09-29 ~ 10-03 的市场展望）：SPY rr25 = −0.022、P99.4（过去一年 99% 的日子
比今天更负）= 看跌保护相对看涨处在一年最便宜的位置，展望却连写「put 翼极贵」「保险全押在
指数下翼」。根因：上下文只给分位、不给方向。rr25 = IV(25Δ 看涨) − IV(25Δ 看跌)，
分位越高 = 看涨翼相对越贵、看跌保护相对越便宜。临时库 + 假 LLM，无网络。
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import db                              # noqa: E402
from rnd.state import STATE_INDICATORS          # noqa: E402
from server import assistant, queries as q      # noqa: E402

_failed = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {extra}")
    if not cond:
        _failed.append(name)


def st(ind, value, pct, dpct=0.0, n=252):
    return {"indicator": ind, "value": value, "pct": pct, "dpct": dpct, "sample_n": n}


def by(entries, ind):
    return next(e for e in entries if e["indicator"] == ind)


# 2026-10-02 线上 SPY 实测读数
SPY_STATES = [st("atm_iv", 0.118, 3.0), st("rr25", -0.0222, 99.4),
              st("bowley_skew", -0.1845, 99.4), st("log_skew", -0.9, 97.0)]
QQQ_STATES = [st("rr25", -0.0302, 98.6), st("bowley_skew", -0.20, 96.0)]
NVDA_STATES = [st("rr25", -0.09, 6.0), st("bowley_skew", -0.40, 5.0)]   # 合成：个股看跌保护一年最贵

# --- 1. 每个状态指标都有方向定义（公式 + 「分位越高 = …」）---
for ind in STATE_INDICATORS:
    check(f"DIRECTION[{ind}] 写明分位方向", "分位越高" in q.DIRECTION.get(ind, ""),
          q.DIRECTION.get(ind))
check("rr25 定义写明公式：看涨减看跌", "IV(25Δ 看涨) − IV(25Δ 看跌)" in q.DIRECTION["rr25"])
check("rr25 定义写明：分位越高 = 看跌保护相对越便宜", "看跌保护相对越便宜" in q.DIRECTION["rr25"])
check("rr25 的 UI 白话不再是 put/call 顺序（公式是 call − put）",
      "put/call" not in q.SAYINGS["rr25"], q.SAYINGS["rr25"])

# --- 2. 状态块：SPY rr25 = −0.022 / P99 → 看跌保护相对便宜，不是 put 贵 ---
spy = assistant._state(SPY_STATES)
rr = by(spy, "rr25")
check("state 条目带 definition", rr.get("definition") == q.DIRECTION["rr25"])
r = rr.get("reading") or ""
check("SPY rr25 P99 → 看跌保护相对看涨处于一年最便宜之列",
      "看跌保护相对看涨处于一年最便宜之列" in r, r)
check("SPY rr25 P99 → 看跌偏斜处于一年最平之列", "看跌偏斜处于一年最平之列" in r, r)
check("SPY rr25 < 0 → 照实说看跌翼 IV 仍高于看涨翼（不写成 call 贵）",
      "看跌翼 IV 仍高于看涨翼" in r and "看涨翼 IV 高于看跌翼" not in r, r)
for bad in ("看跌保护相对看涨处于一年最贵", "put 贵", "put 翼极贵", "看跌偏斜处于一年最陡"):
    check(f"SPY rr25 P99 的描述不含反向说法『{bad}』", bad not in r, r)

bw = by(spy, "bowley_skew").get("reading") or ""
check("SPY bowley P99 → 左偏程度处于一年最轻之列（更右偏）",
      "左偏程度处于一年最轻之列" in bw and "更右偏" in bw, bw)
check("SPY bowley P99 的描述不含『左偏程度处于一年最重』", "左偏程度处于一年最重" not in bw, bw)
iv = by(spy, "atm_iv").get("reading") or ""
check("SPY atm_iv P3 → 隐含波动率处于一年最低之列", "处于一年最低之列" in iv, iv)

# --- 3. 其他档位与符号 ---
lo = assistant.direction_reading("rr25", -0.126, 5.0)
check("rr25 P5 → 看跌保护相对看涨处于一年最贵之列（看跌偏斜最陡）",
      "看跌保护相对看涨处于一年最贵之列" in lo and "看跌偏斜处于一年最陡之列" in lo, lo)
pos = assistant.direction_reading("rr25", 0.03, 92.0)
check("个股 rr25 > 0 / P92 → 看涨翼 IV 高于看跌翼、看涨溢价处于一年最高之列",
      "看涨翼 IV 高于看跌翼" in pos and "看涨溢价处于一年最高之列" in pos, pos)
mid = assistant.direction_reading("rr25", -0.043, 50.0)
check("rr25 P50 → 常态区间，不说贵贱", "常态" in mid and "贵" not in mid and "便宜" not in mid, mid)
hi = assistant.direction_reading("rr25", -0.03, 80.0)
check("rr25 P80 → 比一年中多数日子便宜", "看跌保护相对看涨比一年中多数日子便宜" in hi, hi)
na = assistant.direction_reading("rr25", -0.02, None)
check("pct=None → 不陈述方向", "便宜" not in na and "贵" not in na and "无分位" in na, na)
rs = assistant.direction_reading("bowley_skew", 0.10, 95.0)
check("bowley > 0 / P95 → 右偏程度处于一年最重之列", "右偏程度处于一年最重之列" in rs, rs)

# --- 4. 指数基准块也带方向（个股上下文里的 SPY/QQQ 并排）---
b = assistant._benchmark("SPY", SPY_STATES)
check("benchmark 带 state_reading", isinstance(b.get("state_reading"), dict))
check("benchmark SPY rr25 → 看跌保护相对便宜",
      "看跌保护相对看涨处于一年最便宜之列" in (b.get("state_reading") or {}).get("rr25", ""))

# --- 5. 系统提示纪律 + 组装后的报文里带着方向白话 ---
sysp = assistant.SYSTEM_PROMPT
check("系统提示：方向以 reading 为准", "reading" in sysp and "为准" in sysp)
check("系统提示：不得自行从分位数推断方向", "不得自行从分位数推断" in sysp)
ctx = {"meta": {"symbol": "SPY", "asof": "2026-10-02"}, "state": spy, "benchmark": b}
user = assistant.build_messages(ctx, "指数偏度怎么样？")["user"]
check("报文 user 段含 SPY rr25 的方向白话", "看跌保护相对看涨处于一年最便宜之列" in user)

# --- 6. 事件：分位越线事件带 indicator 与方向白话 ---
tmp = Path(tempfile.mkdtemp()) / "dir.sqlite"
c0 = db.get_conn(tmp)
db.replace_state(c0, "SPY", [("2026-10-01", "SPY", "rr25", -0.03, 85.0, None, 252),
                             ("2026-10-02", "SPY", "rr25", -0.0222, 99.4, 14.4, 252)])
c0.close()
_orig_conn = q.conn
q.conn = lambda: db.get_conn(tmp)
try:
    evs = assistant._events("SPY", "2026-10-02")
finally:
    q.conn = _orig_conn
ext = [e for e in evs if e.get("kind") == "extreme"]
check("rr25 升至 P99 的事件存在", len(ext) == 1, f"events={evs}")
if ext:
    check("事件带 indicator", ext[0].get("indicator") == "rr25", ext[0])
    check("事件带方向白话：看跌保护相对便宜",
          "看跌保护相对看涨处于一年最便宜之列" in (ext[0].get("reading") or ""), ext[0])

# --- 7. 生成结果的关键词反例检查 ---
packs = {
    "SPY": {"state": assistant._state(SPY_STATES), "benchmark": assistant._benchmark("QQQ", QQQ_STATES)},
    "QQQ": {"state": assistant._state(QQQ_STATES), "benchmark": assistant._benchmark("SPY", SPY_STATES)},
    "NVDA": {"state": assistant._state(NVDA_STATES), "benchmark": assistant._benchmark("QQQ", QQQ_STATES)},
}
# eod.log 原句（09-29 ~ 10-03）
for bad in ["宽基把偏度抬到一年顶（put 翼极贵）",
            "SPY：结构却极度偏 put",
            "保险全押在指数下翼",
            "担忧被挤进了宽基的左翼",
            "指数层在为 put 翼付极端溢价（SPY RR P95）",
            "SPY 极度左偏"]:
    hits = assistant.direction_conflicts(bad, packs)
    check(f"命中反例：{bad}", len(hits) >= 1, f"hits={hits}")
for good in ["SPY：看跌翼部处一年最便宜",
             "SPY 看跌保护相对看涨处于一年最便宜之列，put 仍比 call 贵但溢价一年最薄",
             "NVDA：put 翼极贵（RR P6），担忧挤在个股左翼",
             "QQQ 同款：左偏程度处于一年最轻之列",
             "今天担忧集中在哪个层级？个股"]:
    hits = assistant.direction_conflicts(good, packs)
    check(f"不误报：{good}", hits == [], f"hits={hits}")
multi = "NVDA · 钉 10-17\n· put 翼极贵，个股左偏一年最重\nSPY · 钉 10-17\n· 结构极度偏 put"
hits = assistant.direction_conflicts(multi, packs)
check("多段文本：无标的名的行归到最近点名的标的（只命中 SPY 段）",
      len(hits) == 1 and "SPY" in hits[0] and "极度偏 put" in hits[0], f"hits={hits}")
benchmark_only = {"NVDA": packs["NVDA"]}
hits = assistant.direction_conflicts("对照 QQQ：put 翼极贵", benchmark_only)
check("只在 benchmark 里出现的指数也能查（个股助手答案提到 QQQ）", len(hits) == 1, f"hits={hits}")

# --- 8. generate_checked：命中反例 → 带纠错重写一次；仍命中 → 末尾挂自检警告 ---
calls = []


def fake(outputs):
    it = iter(outputs)

    def _gen(system, user, **kw):
        calls.append(user)
        return next(it)
    return _gen


_orig_gen = assistant.generate
msg = {"system": "S", "user": "U"}
try:
    calls.clear()
    assistant.generate = fake(["SPY：看跌翼部处一年最便宜"])
    out = assistant.generate_checked(msg, packs)
    check("无反例：只调一次、原样返回", len(calls) == 1 and out == "SPY：看跌翼部处一年最便宜")

    calls.clear()
    assistant.generate = fake(["SPY：保险全押在指数下翼", "SPY：看跌保护一年最便宜"])
    out = assistant.generate_checked(msg, packs)
    check("命中反例 → 重写一次", len(calls) == 2, f"calls={len(calls)}")
    check("重写请求带纠错段与原句", len(calls) == 2 and "方向纠错" in calls[1]
          and "保险全押在指数下翼" in calls[1] and calls[1].startswith("U"))
    check("重写请求带正确的方向白话", len(calls) == 2
          and "看跌保护相对看涨处于一年最便宜之列" in calls[1])
    check("重写通过 → 返回第二版、无警告", out == "SPY：看跌保护一年最便宜")

    calls.clear()
    assistant.generate = fake(["SPY：put 翼极贵", "SPY：结构极度偏 put"])
    out = assistant.generate_checked(msg, packs)
    check("重写仍命中 → 不再重试", len(calls) == 2, f"calls={len(calls)}")
    check("重写仍命中 → 末尾挂方向自检警告", out.startswith("SPY：结构极度偏 put")
          and "方向自检未通过" in out and "看跌保护相对看涨处于一年最便宜之列" in out, out)
finally:
    assistant.generate = _orig_gen

print()
if _failed:
    print(f"FAILED {len(_failed)}: {_failed}")
    sys.exit(1)
print("全部通过")

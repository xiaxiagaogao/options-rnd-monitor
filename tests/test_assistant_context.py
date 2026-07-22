"""研究助手上下文装配器的验证（对真实库跑，断言结构与口径）。

用法：.venv/bin/python tests/test_assistant_context.py
依赖 worktree 已软链 rnd.sqlite（见 README/环境说明）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import assistant, queries as q

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


# --- 1. 元信息块：symbol / asof（默认最新 pinned 日）/ pinned_expiry / dte ---
ctx = assistant.build_context("SPY")
c = q.conn()
latest = q.latest_date(c, "SPY")
c.close()
meta = ctx.get("meta", {})
check("有 meta 块", isinstance(ctx.get("meta"), dict))
check("meta.symbol == SPY", meta.get("symbol") == "SPY", f"got={meta.get('symbol')}")
check("meta.asof 默认最新 pinned 日", meta.get("asof") == latest,
      f"got={meta.get('asof')} expect={latest}")
check("meta.pinned_expiry 是日期串", isinstance(meta.get("pinned_expiry"), str)
      and len(meta.get("pinned_expiry") or "") == 10, f"got={meta.get('pinned_expiry')}")
check("meta.dte 为正整数", isinstance(meta.get("dte"), int) and meta.get("dte") > 0,
      f"got={meta.get('dte')}")

# --- 2. 定位块：F / ±1σ / Q05–Q95 / mode / 逐分位报价区标志（口径核心）---
loc = ctx.get("location", {})
check("有 location 块", isinstance(ctx.get("location"), dict))
check("location.forward 为正", isinstance(loc.get("forward"), (int, float))
      and loc.get("forward", 0) > 0, f"got={loc.get('forward')}")
check("location 有 sigma1_abs / sigma1_pct",
      loc.get("sigma1_abs") is not None and loc.get("sigma1_pct") is not None,
      f"abs={loc.get('sigma1_abs')} pct={loc.get('sigma1_pct')}")
qs = loc.get("quantiles", {})
check("location.quantiles 含 q05..q95 五档",
      set(qs) == {"q05", "q25", "q50", "q75", "q95"}, f"keys={sorted(qs)}")
check("location.mode 存在", "mode" in loc, f"got={loc.get('mode')}")
inr = loc.get("in_range", {})
check("location.in_range 是逐分位（非单布尔）字典",
      isinstance(inr, dict) and set(inr) == {"q05", "q25", "q50", "q75", "q95"},
      f"keys={sorted(inr) if isinstance(inr, dict) else type(inr).__name__}")
check("in_range 各值为 bool", isinstance(inr, dict)
      and all(isinstance(v, bool) for v in inr.values()), f"vals={inr}")

# --- 3. 状态块：10 个状态指标 × value/pct/dpct/sample_n + saying + label，按序 ---
from rnd.state import STATE_INDICATORS

st = ctx.get("state", [])
check("有 state 块（list）", isinstance(st, list), f"type={type(st).__name__}")
check("state 恰 10 项", len(st) == 10, f"len={len(st)}")
check("state 指标集合 == STATE_INDICATORS",
      {s.get("indicator") for s in st} == set(STATE_INDICATORS))
check("state 顺序 == STATE_INDICATORS",
      [s.get("indicator") for s in st] == list(STATE_INDICATORS))
_need = {"indicator", "value", "pct", "dpct", "sample_n", "saying", "label"}
check("state 每项含全部字段",
      all(_need <= set(s) for s in st),
      f"缺={[sorted(_need - set(s)) for s in st if not _need <= set(s)][:1]}")
check("state.saying 非空（复用 queries.SAYINGS）",
      all((s.get("saying") or "") for s in st))

# --- 4. 质量块：gate_pass / gate_detail / n_modes / modes ---
qual = ctx.get("quality", {})
check("有 quality 块", isinstance(ctx.get("quality"), dict))
check("quality.gate_pass 为 bool", isinstance(qual.get("gate_pass"), bool),
      f"got={qual.get('gate_pass')!r}")
check("quality.gate_detail 为 dict（已解析 JSON）", isinstance(qual.get("gate_detail"), dict))
check("quality.n_modes 为 int", isinstance(qual.get("n_modes"), int), f"got={qual.get('n_modes')}")
check("quality.modes 为 list", isinstance(qual.get("modes"), list))

# --- 5. 对照块：指数基准并排（SPY 的基准 = QQQ）---
bm = ctx.get("benchmark", {})
check("有 benchmark 块", isinstance(ctx.get("benchmark"), dict))
check("benchmark.symbol == QQQ（SPY 基准）", bm.get("symbol") == "QQQ",
      f"got={bm.get('symbol')}")
bsp = bm.get("state_pct", {})
check("benchmark.state_pct 覆盖 10 指标",
      isinstance(bsp, dict) and set(bsp) == set(STATE_INDICATORS),
      f"keys={sorted(bsp) if isinstance(bsp, dict) else type(bsp).__name__}")

# --- 6a. 持仓块：无仓时显式声明（SPY 无开仓）---
p_spy = ctx.get("position", {})
check("有 position 块", isinstance(ctx.get("position"), dict))
check("SPY position.open == False", p_spy.get("open") is False, f"got={p_spy.get('open')}")

# --- 6b. 持仓块：有仓时的身份/止损/冻结线/偏移（NVDA 用户仓，只读）---
ctx_n = assistant.build_context("NVDA")
pn = ctx_n.get("position", {})
c = q.conn()
d_n = q.latest_date(c, "NVDA")
ind_n = q._row(c, "SELECT * FROM rnd_indicators WHERE symbol=? AND date=? AND pinned=1",
               (d_n and "NVDA", d_n))
raw = q.open_positions(c, "NVDA")[0]
c.close()
check("NVDA position.open == True", pn.get("open") is True, f"got={pn.get('open')}")
check("NVDA identity == conviction", pn.get("identity") == raw["identity"],
      f"got={pn.get('identity')}")
check("NVDA stop_q == q05", pn.get("stop_q") == raw["stop_q"], f"got={pn.get('stop_q')}")
check("NVDA frozen 快照含 5 冻结分位 + forward + sigma1",
      isinstance(pn.get("frozen"), dict)
      and {"frozen_q05", "frozen_q50", "frozen_q95", "frozen_forward", "frozen_sigma1"}
      <= set(pn.get("frozen", {})))
# 偏移量 = (现 Q − 冻结 Q) / 冻结 σ1（§2.1 罗盘读数），服务端算好
off = pn.get("offset", {})
sig = raw["frozen_sigma1"]
exp_q50 = (ind_n["q50"] - raw["frozen_q50"]) / sig
check("offset 覆盖 q05..q95", isinstance(off, dict)
      and set(off) == {"q05", "q25", "q50", "q75", "q95"}, f"keys={sorted(off) if isinstance(off, dict) else off}")
check("offset.q50 == (现Q50−冻结Q50)/冻结σ1",
      off.get("q50") is not None and abs(off["q50"] - exp_q50) < 1e-9,
      f"got={off.get('q50')} expect={exp_q50:.6f}")
check("position 有失效判定 invalidated(bool) + stop_level",
      isinstance(pn.get("invalidated"), bool) and pn.get("stop_level") is not None,
      f"inv={pn.get('invalidated')} stop={pn.get('stop_level')}")

# --- 7. 事件块：近窗（与仪表盘 events 同源）---
ev = ctx.get("events")
check("有 events 块（list，键必须存在）",
      "events" in ctx and isinstance(ev, list), f"present={'events' in ctx}")
ev = ev or []
check("events 各项含 date/kind/text",
      all({"date", "kind", "text"} <= set(e) for e in ev) if ev else True,
      f"n={len(ev)}")

# --- 8. 摘要块：PIT 标量 + 闸门 FAIL 时 diagnostics 摘要（gate 过时应为 None）---
summ = ctx.get("summary", {})
check("有 summary 块", isinstance(ctx.get("summary"), dict))
check("summary.pit 为标量 dict（非全网格）", isinstance(summ.get("pit"), dict)
      and "cells" not in summ.get("pit", {}) and "strikes" not in summ.get("pit", {}))
if qual.get("gate_pass") is True:
    check("闸门通过 → summary.gate_diagnostics 为 None",
          summ.get("gate_diagnostics") is None, f"got={summ.get('gate_diagnostics')}")

# --- 9. 健壮性：未知标的不崩，显式 unavailable ---
try:
    z = assistant.build_context("ZZZZ")
    raised = None
except Exception as e:  # noqa: BLE001
    z, raised = {}, e
check("未知标的不抛异常", raised is None, f"exc={raised!r}")
check("未知标的 meta.symbol 回显 + asof 为 None",
      z.get("meta", {}).get("symbol") == "ZZZZ" and z.get("meta", {}).get("asof") is None)
check("未知标的 location.available == False",
      z.get("location", {}).get("available") is False)
check("未知标的 position.open == False", z.get("position", {}).get("open") is False)


if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

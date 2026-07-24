"""持仓同步验证（合成 fund.db + 临时 yaml + 合成 journal）。
用法：.venv/bin/python tests/test_holdings_sync.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


def _write_yaml(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return Path(f.name)


# === 1. pool schema：read/write/effective ===
from server import pool

yml = _write_yaml(
    "baseline:\n  - SPY\n  - QQQ\nholdings:\n  - AAPL\npinned:\n  - NVDA\n")
p = pool.read_pool(yml)
check("read_pool 三组键", set(p) >= {"baseline", "holdings", "pinned"}, f"keys={set(p)}")
check("baseline 读出", p["baseline"] == ["SPY", "QQQ"], f"{p['baseline']}")
check("holdings 读出", p["holdings"] == ["AAPL"], f"{p['holdings']}")
check("pinned 读出", p["pinned"] == ["NVDA"], f"{p['pinned']}")

pool.write_pool(["SPY", "QQQ"], ["AAPL", "MU"], ["NVDA"], yml)
p2 = pool.read_pool(yml)
check("write_pool 回读 holdings", p2["holdings"] == ["AAPL", "MU"], f"{p2['holdings']}")

eff = pool.effective_symbols(yml)
check("effective 去重并集", set(eff) == {"SPY", "QQQ", "AAPL", "MU", "NVDA"}, f"{eff}")
check("effective 无重复", len(eff) == len(set(eff)), f"{eff}")


# === 2. map_symbol + 黑名单 ===
from rnd import holdings_sync as hs

check("NVDAUSDT → NVDA", hs.map_symbol("NVDAUSDT") == "NVDA")
check("SPYUSDT → SPY", hs.map_symbol("SPYUSDT") == "SPY")
check("小写归一", hs.map_symbol("aaplusdt") == "AAPL")
check("SAMSUNGUSDT 黑名单→None", hs.map_symbol("SAMSUNGUSDT") is None)
check("BZUSDT 布伦特→None（非美股 BZ）", hs.map_symbol("BZUSDT") is None)
check("XAUUSDT 商品→None", hs.map_symbol("XAUUSDT") is None)
check("非 USDT 结尾→None", hs.map_symbol("NVDABUSD") is None)
check("USDT 单独→None", hs.map_symbol("USDT") is None)
check("空串→None", hs.map_symbol("") is None)


# === 3. derive_current_holdings + resolve_holdings ===
def _make_fund_db(fills) -> Path:
    """fills: list of (symbol, position_side, side, qty)。返回临时 fund.db 路径。"""
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.execute("CREATE TABLE binance_fills "
                 "(symbol TEXT, position_side TEXT, side TEXT, qty REAL)")
    conn.executemany("INSERT INTO binance_fills VALUES (?,?,?,?)", fills)
    conn.commit()
    conn.close()
    return Path(f.name)

db_hold = _make_fund_db([
    ("NVDAUSDT", "LONG", "BUY", 1.0), ("NVDAUSDT", "LONG", "BUY", 0.2),   # net 1.2 持有
    ("INTCUSDT", "LONG", "BUY", 0.5), ("INTCUSDT", "LONG", "SELL", 0.5),  # net 0 已平
    ("GOOGLUSDT", "LONG", "BUY", 0.26),                                    # 持有
    ("TSLAUSDT", "LONG", "BUY", 1.0), ("TSLAUSDT", "SHORT", "SELL", 1.0),  # 对冲两腿都持有
    ("SAMSUNGUSDT", "LONG", "BUY", 0.24),                                  # 持有但黑名单
])
held = hs.derive_current_holdings(db_hold)
check("净持仓集合", held == {"NVDAUSDT", "GOOGLUSDT", "TSLAUSDT", "SAMSUNGUSDT"},
      f"{held}")
check("已平仓不计（INTC）", "INTCUSDT" not in held)
check("对冲不抵消（TSLA 两腿）", "TSLAUSDT" in held)

check("本机无 fund.db → 空集", hs.derive_current_holdings("/nonexistent/fund.db") == set())

mapped, excluded = hs.resolve_holdings(db_hold)
check("resolve 映射美股", mapped == {"NVDA", "GOOGL", "TSLA"}, f"{mapped}")
check("resolve 排除黑名单", excluded == {"SAMSUNGUSDT"}, f"{excluded}")


# === 4. journal.open_symbols ===
from rnd import db as rnddb, journal

def _make_rnd_db_with_journal(events) -> "sqlite3.Connection":
    """events: list of (position_id, event_type, symbol)。返回内存 rnd conn。"""
    f = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    f.close()
    conn = rnddb.get_conn(Path(f.name))
    conn.executemany(
        "INSERT INTO trade_journal (position_id, event_type, event_date, symbol) "
        "VALUES (?,?,?,?)",
        [(pid, et, "2026-07-24", sym) for pid, et, sym in events])
    conn.commit()
    return conn

jc = _make_rnd_db_with_journal([
    ("p1", "open", "NVDA"),                 # 未平 → open
    ("p2", "open", "SPY"), ("p2", "close", "SPY"),  # 已平 → 不算
    ("p3", "open", "AMD"), ("p3", "roll_repin", "AMD"),  # roll 后仍未平 → open
])
osym = journal.open_symbols(jc)
jc.close()
check("open_symbols 只含未平", osym == {"NVDA", "AMD"}, f"{osym}")


# === 5. sync 编排 ===
# 假闸门：除 RKLB/FLNC 外都放行（模拟中小盘被链质量挡）。
fake_gate = lambda s: s not in {"RKLB", "FLNC"}

# fund.db 当前持仓：NVDA(pinned+持有)/GOOGL(旧holdings)/AAPL(新,过)/RKLB(新,拒)/SAMSUNG(排除)
db5 = _make_fund_db([
    ("NVDAUSDT", "LONG", "BUY", 1.2),
    ("GOOGLUSDT", "LONG", "BUY", 0.26),
    ("AAPLUSDT", "LONG", "BUY", 0.5),
    ("RKLBUSDT", "LONG", "BUY", 0.41),
    ("SAMSUNGUSDT", "LONG", "BUY", 0.24),
])
yml5 = _write_yaml("baseline:\n  - SPY\n  - QQQ\nholdings:\n  - GOOGL\n  - MU\npinned:\n  - NVDA\n")
jc5 = _make_rnd_db_with_journal([("p1", "open", "NVDA")])

res = hs.sync(jc5, fund_db_path=db5, gate_fn=fake_gate, yaml_path=yml5)
jc5.close()
after = pool.read_pool(yml5)
check("新标的过闸门→added", res["added"] == ["AAPL"], f"{res['added']}")
check("闸门拒→rejected", res["rejected"] == ["RKLB"], f"{res['rejected']}")
check("平仓且无 journal→removed（MU）", res["removed"] == ["MU"], f"{res['removed']}")
check("黑名单→excluded", res["excluded"] == ["SAMSUNGUSDT"], f"{res['excluded']}")
check("pinned 保留 NVDA", res["pinned"] == ["NVDA"], f"{res['pinned']}")
check("holdings 写入 = 仍持有旧+新过闸门", set(after["holdings"]) == {"GOOGL", "AAPL"},
      f"{after['holdings']}")
check("baseline 不动", after["baseline"] == ["SPY", "QQQ"], f"{after['baseline']}")
check("NVDA 不重复进 holdings（在 pinned）", "NVDA" not in after["holdings"])

# 本机无 fund.db → skipped，池不动
yml_skip = _write_yaml("baseline:\n  - SPY\nholdings:\n  - AAPL\npinned: []\n")
res_skip = hs.sync(_make_rnd_db_with_journal([]), fund_db_path="/nonexistent.db",
                   gate_fn=fake_gate, yaml_path=yml_skip)
check("无库→skipped", "skipped" in res_skip, f"{res_skip}")
check("skipped 不动池", pool.read_pool(yml_skip)["holdings"] == ["AAPL"])

# 5a. holdings→pinned 降级（隔离测 new_holdings -= pinned 那行）
db5b = _make_fund_db([
    ("NVDAUSDT", "LONG", "BUY", 1.0),
    ("GOOGLUSDT", "LONG", "BUY", 0.3),
])
yml5b = _write_yaml("baseline:\n  - SPY\nholdings:\n  - NVDA\n  - GOOGL\npinned: []\n")
jc5b = _make_rnd_db_with_journal([("p1", "open", "NVDA")])
res5b = hs.sync(jc5b, fund_db_path=db5b, gate_fn=fake_gate, yaml_path=yml5b)
jc5b.close()
after5b = pool.read_pool(yml5b)
check("holdings→pinned 降级：NVDA 退出 holdings", "NVDA" not in after5b["holdings"],
      f"{after5b['holdings']}")
check("holdings→pinned 降级：pinned=NVDA", res5b["pinned"] == ["NVDA"], f"{res5b['pinned']}")
check("holdings→pinned 降级：不计入 added", "NVDA" not in res5b["added"], f"{res5b['added']}")
check("holdings→pinned 降级：不计入 removed", "NVDA" not in res5b["removed"], f"{res5b['removed']}")

# 5b. gate_fn 逐标的异常隔离：一个标的抛异常不堵死其他候选
def raise_gate(s):
    if s == "AAPL":
        raise RuntimeError("boom")
    return True

db5c = _make_fund_db([
    ("AAPLUSDT", "LONG", "BUY", 0.5),
    ("MSFTUSDT", "LONG", "BUY", 0.4),
])
yml5c = _write_yaml("baseline:\n  - SPY\nholdings: []\npinned: []\n")
jc5c = _make_rnd_db_with_journal([])
res5c = hs.sync(jc5c, fund_db_path=db5c, gate_fn=raise_gate, yaml_path=yml5c)
jc5c.close()
after5c = pool.read_pool(yml5c)
check("gate 异常隔离：AAPL 进 gate_errors", res5c["gate_errors"] == ["AAPL"],
      f"{res5c['gate_errors']}")
check("gate 异常隔离：MSFT 仍 added", "MSFT" in res5c["added"], f"{res5c['added']}")
check("gate 异常隔离：yaml 正常写入（未被异常堵死）", "MSFT" in after5c["holdings"],
      f"{after5c['holdings']}")


# === 6. pinned→holdings 降级不重复过闸门/不误报（final review gap 2）===
_gate_calls6 = []
def _recording_gate6(s):
    _gate_calls6.append(s)
    return True
# fund.db 仍持有 NVDA；盘上 pinned=[NVDA]（昨天 journal 写的）；今天 journal 已平仓
db6 = _make_fund_db([("NVDAUSDT", "LONG", "BUY", 1.2)])
yml6 = _write_yaml("baseline:\n  - SPY\nholdings: []\npinned:\n  - NVDA\n")
jc6 = _make_rnd_db_with_journal([])   # 无 open journal
res6 = hs.sync(jc6, fund_db_path=db6, gate_fn=_recording_gate6, yaml_path=yml6)
jc6.close()
after6 = pool.read_pool(yml6)
check("降级不重复 gate", "NVDA" not in _gate_calls6, f"gate_calls={_gate_calls6}")
check("降级不误报 added", "NVDA" not in res6["added"], f"added={res6['added']}")
check("降级进 holdings", after6["holdings"] == ["NVDA"], f"{after6['holdings']}")
check("journal 平仓后 pinned 空", after6["pinned"] == [], f"{after6['pinned']}")


# === 末尾判定 ===
if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

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


# === 末尾判定 ===
if failures:
    print(f"\n{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("\n全部通过。")

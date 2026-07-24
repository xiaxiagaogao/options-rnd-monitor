"""标的池管理：baseline/holdings/pinned 三组 + 派发后台回填 + 进度跟踪。

holdings-sync-framework §4.2：baseline 恒在 + holdings 持仓驱动 + pinned 强保留。
MAX_DYNAMIC 仅约束手动 swap_in 这个后门（正常 holdings 由 rnd/holdings_sync.py 每日重写）。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from rnd.config import PROJECT_ROOT

MAX_DYNAMIC = 2
_JOBS: dict[str, dict] = {}   # symbol -> {pid, log}

_YAML_TEMPLATE = """# 标的池（holdings-sync-framework §4.2）：baseline 恒在 + holdings 持仓驱动 + pinned 强保留。
# holdings 与 pinned 均由 rnd/holdings_sync.py 每日自动重写，勿手动编辑。
# （要手动保住一个标的：建一条 open trade_journal 条目，或用「添加标的」admin 后门）
baseline:
{baseline}
holdings:
{holdings}
pinned:
{pinned}

# 到期日规则（spec §1）
expiry:
  monthly_only: true
  dte_min: 7
  dte_max: 60
  count: 2
"""


def _yaml_path(yaml_path=None):
    return Path(yaml_path) if yaml_path else (PROJECT_ROOT / "symbols.yaml")


def read_pool(yaml_path=None) -> dict:
    """读取标的池：返回 {baseline, holdings, pinned} 三组 list[str]。"""
    cfg = yaml.safe_load(_yaml_path(yaml_path).read_text())
    return {
        "baseline": list(cfg.get("baseline") or []),
        "holdings": list(cfg.get("holdings") or []),
        "pinned": list(cfg.get("pinned") or []),
    }


def _fmt(items):
    return "\n".join(f"  - {s}" for s in items) if items else "  []"


def write_pool(baseline: list[str], holdings: list[str], pinned: list[str], yaml_path=None):
    """写入标的池：baseline/holdings/pinned 三组整体重写 yaml_path（默认 symbols.yaml）。"""
    _yaml_path(yaml_path).write_text(_YAML_TEMPLATE.format(
        baseline=_fmt(baseline), holdings=_fmt(holdings), pinned=_fmt(pinned)))


def effective_symbols(yaml_path=None) -> list[str]:
    """有效池 = baseline ∪ holdings ∪ pinned，去重保序。"""
    p = read_pool(yaml_path)
    out = []
    for group in (p["baseline"], p["holdings"], p["pinned"]):
        for s in group:
            if s not in out:
                out.append(s)
    return out


def swap_in(symbol: str, replace: str | None = None) -> dict:
    symbol = symbol.upper()
    p = read_pool()
    if symbol in p["baseline"] + p["holdings"] + p["pinned"]:
        return {"ok": False, "error": f"{symbol} 已在池中"}
    holdings = p["holdings"]
    if len(holdings) >= MAX_DYNAMIC:
        if not replace or replace.upper() not in holdings:
            return {"ok": False, "error": "持仓组已满，需指定换出哪一个",
                    "holdings": holdings}
        holdings = [s for s in holdings if s != replace.upper()]
    holdings.append(symbol)
    write_pool(p["baseline"], holdings, p["pinned"])
    job = _spawn_backfill(symbol)
    return {"ok": True, "holdings": holdings, "swapped_out": replace, "backfill": job}


def _spawn_backfill(symbol: str) -> dict:
    log = PROJECT_ROOT / "output" / f"backfill_{symbol}.log"
    log.parent.mkdir(exist_ok=True)
    with open(log, "ab") as fh:
        p = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "backfill.py"),
             "--symbols", symbol, "--years", "3"],
            stdout=fh, stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT, start_new_session=True)
    _JOBS[symbol] = {"pid": p.pid, "log": str(log)}
    return {"pid": p.pid, "log": log.name}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def status() -> dict:
    from rnd import db
    conn = db.get_conn()
    p = read_pool()
    out = []
    for sym in p["holdings"]:
        rows, latest = conn.execute(
            "SELECT COUNT(*), MAX(date) FROM rnd_indicators WHERE symbol=?",
            (sym,)).fetchone()
        job = _JOBS.get(sym)
        running = bool(job and _pid_alive(job["pid"]))
        progress = None
        log_path = PROJECT_ROOT / "output" / f"backfill_{sym}.log"
        if log_path.exists():
            tail = log_path.read_text()[-2000:]
            m = re.findall(r"\[(\d+)/(\d+)\]", tail)
            if m:
                progress = f"拉取 {m[-1][0]}/{m[-1][1]}"
            if "回填完成" in tail:
                progress = "完成"
        out.append({"symbol": sym, "rows": rows, "latest": latest,
                    "running": running, "progress": progress})
    conn.close()
    return {"holdings": out, "count": len(p["holdings"])}

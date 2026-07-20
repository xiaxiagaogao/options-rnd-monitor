"""动态槽池管理：换入/换出 symbols.yaml + 派发后台回填 + 进度跟踪。

spec §2：动态槽 2 个；换入前强制准入检查；换入即回填；换出不删数据。
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

_YAML_TEMPLATE = """# 标的池（spec §1/§2）：固定 3 + 动态 2。换出不删数据。
fixed:
{fixed}
dynamic:
  # 动态槽换入前必须通过准入检查（spec §2，rnd/admission.py）：
  # ATM 邻域相对点差中位数 ≤ 3%；±20% moneyness 双边报价行权价 ≥ 15 个
{dynamic}

# 到期日规则（spec §1）
expiry:
  monthly_only: true
  dte_min: 7
  dte_max: 60
  count: 2          # 最近两个月度各算一套
"""


def read_pool() -> dict:
    cfg = yaml.safe_load((PROJECT_ROOT / "symbols.yaml").read_text())
    return {"fixed": list(cfg.get("fixed", [])), "dynamic": list(cfg.get("dynamic") or [])}


def _write_pool(fixed: list[str], dynamic: list[str]):
    fixed_s = "\n".join(f"  - {s}" for s in fixed)
    dynamic_s = "\n".join(f"  - {s}" for s in dynamic) if dynamic else "  []"
    (PROJECT_ROOT / "symbols.yaml").write_text(
        _YAML_TEMPLATE.format(fixed=fixed_s, dynamic=dynamic_s))


def swap_in(symbol: str, replace: str | None = None) -> dict:
    symbol = symbol.upper()
    pool = read_pool()
    if symbol in pool["fixed"] + pool["dynamic"]:
        return {"ok": False, "error": f"{symbol} 已在池中"}
    dynamic = pool["dynamic"]
    if len(dynamic) >= MAX_DYNAMIC:
        if not replace or replace.upper() not in dynamic:
            return {"ok": False, "error": "动态槽已满，需指定换出哪一个",
                    "dynamic": dynamic}
        dynamic = [s for s in dynamic if s != replace.upper()]
    dynamic.append(symbol)
    _write_pool(pool["fixed"], dynamic)
    job = _spawn_backfill(symbol)
    return {"ok": True, "dynamic": dynamic, "swapped_out": replace, "backfill": job}


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
    pool = read_pool()
    out = []
    for sym in pool["dynamic"]:
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
    return {"dynamic": out, "slots": f"{len(pool['dynamic'])}/{MAX_DYNAMIC}"}

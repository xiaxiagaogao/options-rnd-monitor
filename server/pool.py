"""标的池（watchlist spec，2026-11-07）：池 = 自选清单里 stage=ready 的条目。

自选清单在 rnd.sqlite（rnd/watchlist.py），用户在面板「自选清单」页维护；持仓同步不再改池。
symbols.yaml 只剩 expiry 段在用（rnd/config.EXPIRY）；它的 baseline/holdings/pinned 三组
仅作为「清单从没写过（未迁移）」时的退路，以及迁移脚本的播种来源。
"""
from pathlib import Path

import yaml

from rnd.config import EXPIRY, PROJECT_ROOT

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
  monthly_only: {monthly_only}
  dte_min: {dte_min}
  dte_max: {dte_max}
  count: {count}
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
    """写入标的池：baseline/holdings/pinned 三组整体重写 yaml_path（默认 symbols.yaml）。

    expiry 段按 config.EXPIRY 原样写回——这里曾写死 dte_min: 7，而 holdings_sync
    每日重写 symbols.yaml，任何对到期窗口的修改都会被静默还原（改完次日即失效）。"""
    _yaml_path(yaml_path).write_text(_YAML_TEMPLATE.format(
        baseline=_fmt(baseline), holdings=_fmt(holdings), pinned=_fmt(pinned),
        monthly_only=str(EXPIRY["monthly_only"]).lower(), dte_min=EXPIRY["dte_min"],
        dte_max=EXPIRY["dte_max"], count=EXPIRY["count"]))


def yaml_symbols(yaml_path=None) -> list[str]:
    """symbols.yaml 的旧池 = baseline ∪ holdings ∪ pinned，去重保序（迁移播种 / 未迁移退路）。"""
    p = read_pool(yaml_path)
    out = []
    for group in (p["baseline"], p["holdings"], p["pinned"]):
        for s in group:
            if s not in out:
                out.append(s)
    return out


def effective_symbols() -> list[str]:
    """有效池 = 自选清单里 ready 的标的。清单从没写过（未迁移）→ 退回 symbols.yaml，免得池突然变空。"""
    from rnd import db, watchlist
    conn = db.get_conn()
    try:
        if not watchlist.is_seeded(conn):
            return yaml_symbols()
        return watchlist.ready_tickers(conn)
    finally:
        conn.close()

"""迁移脚本（watchlist spec §8）：旧 CHECK 的身份表 + yaml 池 → 清单。临时库，不触网。

    .venv/bin/python tests/test_migrate_watchlist.py
"""
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from rnd import db, identity as ident, watchlist as wl   # noqa: E402
import migrate_watchlist as mig                           # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


OLD_DDL = """CREATE TABLE holding_identity (
  event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol        TEXT NOT NULL,
  identity      TEXT NOT NULL CHECK (identity IN ('speculative','conviction')),
  prev_identity TEXT,
  changed_at    TEXT NOT NULL
);"""
ROWS = [("QQQ", "conviction", None, "2026-10-19T22:30:00+08:00"),
        ("NVDA", "speculative", None, "2026-10-19T22:31:00+08:00"),
        ("NVDA", "conviction", "speculative", "2026-10-20T09:00:00+08:00"),
        ("AVGO", "speculative", None, "2026-10-19T22:30:30+08:00")]   # 已出池，身份照样保留
YAML = """baseline:
  - SPY
  - QQQ
holdings:
  - MU
  - NVDA
pinned:
  []

expiry:
  monthly_only: true
  dte_min: 2
  dte_max: 60
  count: 2
"""

with tempfile.TemporaryDirectory() as tmp:
    path, yml = Path(tmp) / "old.sqlite", Path(tmp) / "symbols.yaml"
    yml.write_text(YAML)
    raw = sqlite3.connect(path)
    raw.executescript(OLD_DDL)
    raw.executemany("INSERT INTO holding_identity (symbol, identity, prev_identity, changed_at) VALUES (?,?,?,?)", ROWS)
    raw.execute("DELETE FROM holding_identity WHERE event_id = 2")   # 制造 event_id 空洞，验证原样拷贝
    raw.commit()
    raw.close()
    conn = db.get_conn(path)
    for t in ("SPY", "QQQ", "NVDA"):     # MU 没有 pinned 数据 → 播种为 queued
        db.upsert_indicators(conn, {"date": "2026-11-06", "symbol": t, "expiry": "2026-11-20",
                                    "dte": 14, "gate_pass": 1, "pinned": 1, "roll": 0})
    conn.commit()
    before = conn.execute("SELECT event_id, symbol, identity, prev_identity, changed_at FROM holding_identity"
                          " ORDER BY event_id").fetchall()

    print("[dry-run]")
    p = mig.plan(conn, yml)
    check("旧 CHECK 需要重建", p["rebuild_identity"] is True)
    check("MU 没有身份 → 中止", p["missing_role"] == ["MU"], str(p["missing_role"]))
    blocked = None
    try:
        mig.apply(conn, yml)
    except mig.MigrationBlocked as e:
        blocked = e
    check("缺身份时 apply 拒绝且不动库", blocked is not None and mig.identity_needs_rebuild(conn)
          and not wl.is_seeded(conn))

    print("[apply]")
    ident_conn = conn
    ident_conn.execute("INSERT INTO holding_identity (symbol, identity, prev_identity, changed_at)"
                       " VALUES ('MU', 'conviction', NULL, '2026-10-19T22:31:30+08:00')")
    ident_conn.commit()
    before = conn.execute("SELECT event_id, symbol, identity, prev_identity, changed_at FROM holding_identity"
                          " ORDER BY event_id").fetchall()
    p = mig.apply(conn, yml)
    rows = conn.execute("SELECT event_id, symbol, identity, prev_identity, changed_at FROM holding_identity"
                        " ORDER BY event_id").fetchall()
    check("原有身份行原样保留（含 event_id 空洞）", rows[:len(before)] == before, str(rows[:len(before)]))
    check("CHECK 已放开", not mig.identity_needs_rebuild(conn))
    check("新增行接在最大 event_id 之后", rows[len(before)][0] > before[-1][0], str(rows[len(before):]))
    st = {e["ticker"]: e for e in wl.entries(conn)}
    check("按 yaml 池播种 4 条", sorted(st) == ["MU", "NVDA", "QQQ", "SPY"], str(sorted(st)))
    check("SPY → 指数（新登记）", st["SPY"]["role"] == "index" and ident.history(conn, "SPY")[0]["prev_identity"] is None)
    check("QQQ → 由信念改为指数", st["QQQ"]["role"] == "index"
          and [h["identity"] for h in ident.history(conn, "QQQ")] == ["index", "conviction"])
    check("NVDA 沿用信念，不新增行", st["NVDA"]["role"] == "conviction" and len(ident.history(conn, "NVDA")) == 1)
    check("有数据 → ready，没数据 → queued", st["NVDA"]["stage"] == "ready" and st["MU"]["stage"] == "queued")
    check("池 = ready（SPY/QQQ 在前）", wl.ready_tickers(conn) == ["SPY", "QQQ", "NVDA"], str(wl.ready_tickers(conn)))
    check("已出池的 AVGO 身份保留、不进清单", "AVGO" not in st and ident.current(conn)["AVGO"]["identity"] == "speculative")
    check("回放：迁移前的日期 QQQ 仍是信念", (ident.as_of(conn, "QQQ", "2026-10-25") or {}).get("identity") == "conviction")
    n_events = conn.execute("SELECT COUNT(*) FROM watchlist_events").fetchone()[0]

    print("[再跑一次]")
    p2 = mig.apply(conn, yml)
    check("二次运行无变化", not p2["rebuild_identity"] and p2["seeds"] == []
          and conn.execute("SELECT COUNT(*) FROM watchlist_events").fetchone()[0] == n_events)
    conn.close()

print("[高水位 / 播种原子性]")
with tempfile.TemporaryDirectory() as tmp:
    path, yml = Path(tmp) / "hw.sqlite", Path(tmp) / "symbols.yaml"
    yml.write_text(YAML)
    raw = sqlite3.connect(path)
    raw.executescript(OLD_DDL)
    raw.executemany("INSERT INTO holding_identity (symbol, identity, prev_identity, changed_at) VALUES (?,?,?,?)",
                    [("QQQ", "conviction", None, "t1"), ("NVDA", "conviction", None, "t2"),
                     ("MU", "conviction", None, "t3"), ("ZZZ", "speculative", None, "t4")])
    raw.execute("DELETE FROM holding_identity WHERE symbol = 'ZZZ'")    # 删掉最大 id（4）
    raw.commit()
    raw.close()
    conn = db.get_conn(path)
    mig.rebuild_identity(conn)
    conn.execute("INSERT INTO holding_identity (symbol, identity, changed_at) VALUES ('X', 'speculative', 't5')")
    nid = conn.execute("SELECT MAX(event_id) FROM holding_identity").fetchone()[0]
    check("删掉的最大 id 不被重用（下一行是 5 而不是 4）", nid == 5, f"next={nid}")
    conn.execute("DELETE FROM holding_identity WHERE symbol = 'X'")
    conn.commit()

    real_add = wl.add
    calls = []

    def flaky_add(c, t, *a, **k):
        calls.append(t)
        if t == "MU":
            raise RuntimeError("中途出错")
        return real_add(c, t, *a, **k)

    mig.wl.add = flaky_add
    try:
        mig.apply(conn, yml)
    except RuntimeError:
        pass
    finally:
        mig.wl.add = real_add
    check("播种中途出错 → 全部回滚，清单仍未写过", not wl.is_seeded(conn) and wl.entries(conn) == [],
          f"seeded={wl.is_seeded(conn)}")
    mig.apply(conn, yml)
    check("重跑后完整播种 4 条", sorted(e["ticker"] for e in wl.entries(conn)) == ["MU", "NVDA", "QQQ", "SPY"])
    conn.close()

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过。")

"""一次性迁移到自选清单（watchlist spec §8）。默认 dry-run 只打印计划，--apply 才写库；可重复跑。

1. holding_identity 的 CHECK 不含 index → 事务内重建表（按原 event_id 原样拷贝全部行）。
2. 清单从没写过 → 按 symbols.yaml 当前有效池（baseline ∪ holdings ∪ pinned）播种：
   有 pinned 数据的直接 ready（进池），否则 queued（交给 worker 准入 + 回填）。
3. 角色：SPY / QQQ → index（用户 2026-11-07 定），其余沿用 holding_identity 最新身份；
   有标的没有身份 → 列出并中止，等人定。

动生产库前先 .backup，并停掉 rnd-dashboard（重建表要写锁）。
    .venv/bin/python scripts/migrate_watchlist.py [--apply] [--db PATH] [--yaml PATH]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import db, identity as ident, watchlist as wl   # noqa: E402
from server import pool                                   # noqa: E402

ROLE_OVERRIDES = {"SPY": "index", "QQQ": "index"}   # 用户 2026-11-07 定：QQQ 由信念改为指数

_NEW_IDENTITY_DDL = """CREATE TABLE holding_identity_new (
  event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol        TEXT NOT NULL,
  identity      TEXT NOT NULL CHECK (identity IN ('speculative','conviction','index')),
  prev_identity TEXT,            -- 改动前身份；首次登记为 NULL
  changed_at    TEXT NOT NULL    -- 改动时刻，ISO8601 带服务器时区偏移
)"""


class MigrationBlocked(RuntimeError):
    """有标的没有身份，需要人定角色后再迁移。"""


def identity_needs_rebuild(conn) -> bool:
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='holding_identity'").fetchone()
    return sql is not None and "'index'" not in sql[0]


def rebuild_identity(conn):
    """放开 CHECK：建新表 → 原样拷贝（含 event_id）→ 删旧表 → 改名，单事务。
    AUTOINCREMENT 高水位一并保留：被删过的最大 id 不能在新表里被重用。"""
    n_before = conn.execute("SELECT COUNT(*) FROM holding_identity").fetchone()[0]
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'holding_identity'").fetchone()
    old_seq = row[0] if row else 0
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_NEW_IDENTITY_DDL)
        conn.execute("INSERT INTO holding_identity_new (event_id, symbol, identity, prev_identity, changed_at)"
                     " SELECT event_id, symbol, identity, prev_identity, changed_at FROM holding_identity"
                     " ORDER BY event_id")
        conn.execute("DROP TABLE holding_identity")
        conn.execute("ALTER TABLE holding_identity_new RENAME TO holding_identity")
        n_after = conn.execute("SELECT COUNT(*) FROM holding_identity").fetchone()[0]
        if n_after != n_before:
            raise RuntimeError(f"重建后行数不符：{n_before} → {n_after}")
        max_id = conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM holding_identity").fetchone()[0]
        seq = max(old_seq, max_id)
        if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name = 'holding_identity'").fetchone():
            conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'holding_identity'", (seq,))
        elif seq:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('holding_identity', ?)", (seq,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def plan(conn, yaml_path=None) -> dict:
    """算出要做什么，不写库。"""
    current = ident.current(conn)
    seeds, missing = [], []
    if not wl.is_seeded(conn):
        for t in pool.yaml_symbols(yaml_path):
            role = ROLE_OVERRIDES.get(t) or (current.get(t) or {}).get("identity")
            if role is None:
                missing.append(t)
                continue
            has_data = conn.execute("SELECT 1 FROM rnd_indicators WHERE symbol=? AND pinned=1 LIMIT 1",
                                    (t,)).fetchone() is not None
            seeds.append({"ticker": t, "role": role, "prev_role": (current.get(t) or {}).get("identity"),
                          "binance": wl.default_binance(t), "stage": "ready" if has_data else "queued"})
    return {"rebuild_identity": identity_needs_rebuild(conn), "seeds": seeds, "missing_role": missing,
            "already_seeded": wl.is_seeded(conn)}


def apply(conn, yaml_path=None) -> dict:
    p = plan(conn, yaml_path)
    if p["missing_role"]:
        raise MigrationBlocked(f"这些标的没有身份，先定角色再迁移：{p['missing_role']}")
    if p["rebuild_identity"]:
        rebuild_identity(conn)
    # 播种整体一个事务：半途失败就全部回滚，下次重跑仍会完整播种（is_seeded 只看有没有流水）
    try:
        for s in p["seeds"]:
            wl.add(conn, s["ticker"], s["role"], s["binance"], stage=s["stage"], source="migration",
                   commit=False)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--yaml")
    args = ap.parse_args()
    conn = db.get_conn(Path(args.db)) if args.db else db.get_conn()
    try:
        p = plan(conn, args.yaml)
        print(f"holding_identity 需要放开 CHECK（加入 index）：{p['rebuild_identity']}")
        if p["already_seeded"]:
            print("清单已写过，不再播种。")
        for s in p["seeds"]:
            change = "" if s["prev_role"] == s["role"] else f"（身份 {s['prev_role']} → {s['role']}）"
            print(f"  播种 {s['ticker']:6} role={s['role']:11} binance={s['binance']:12} stage={s['stage']}{change}")
        if p["missing_role"]:
            print(f"中止：这些标的没有身份 {p['missing_role']}")
            sys.exit(2)
        if not args.apply:
            print("dry-run 完成（加 --apply 写库）。")
            return
        apply(conn, args.yaml)
        print(f"已迁移：清单 {len(wl.entries(conn))} 条，池 {wl.ready_tickers(conn)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

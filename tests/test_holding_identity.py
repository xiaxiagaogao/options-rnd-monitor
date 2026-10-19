"""币安持仓身份登记（holding-identity spec §4–§6.1）。临时库，不触网。

    .venv/bin/python tests/test_holding_identity.py
"""
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import db, identity as ident

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


SGT = timezone(timedelta(hours=8))
T1 = datetime(2026, 10, 19, 9, 30, tzinfo=SGT)
T2 = datetime(2026, 10, 25, 21, 5, tzinfo=SGT)

print("[选线] stop_quantile / line_pair")
for (idn, long), want in {("speculative", True): "q25", ("conviction", True): "q05",
                          ("speculative", False): "q75", ("conviction", False): "q95",
                          (None, True): None, (None, False): None}.items():
    got = ident.stop_quantile(idn, long)
    check(f"{idn}/{'多' if long else '空'} → {want}", got == want, f"got={got}")
check("line_pair 多头 = 投机 q25 + 信念 q05",
      ident.line_pair(True) == {"speculative": "q25", "conviction": "q05"}, str(ident.line_pair(True)))
check("line_pair 空头 = 投机 q75 + 信念 q95",
      ident.line_pair(False) == {"speculative": "q75", "conviction": "q95"}, str(ident.line_pair(False)))

print("[记录] 登记 / 重复 / 改动 / 历史")
with tempfile.TemporaryDirectory() as tmp:
    conn = db.get_conn(Path(tmp) / "t.sqlite")
    check("新库无登记", ident.current(conn) == {} and ident.history(conn, "AAA") == [])

    r = ident.set_identity(conn, "aaa", "conviction", now=T1)
    check("首次登记：changed、prev 为 None、代码转大写",
          r["changed"] is True and r["prev_identity"] is None and r["symbol"] == "AAA", str(r))
    check("首次登记文案", ident.change_note(r) == "10-19 登记为信念", str(ident.change_note(r)))

    r = ident.set_identity(conn, "AAA", "conviction", now=T2)
    n = conn.execute("SELECT COUNT(*) FROM holding_identity").fetchone()[0]
    check("相同身份 → no-op 不写行", r["changed"] is False and n == 1, f"rows={n}")

    r = ident.set_identity(conn, "AAA", "speculative", now=T2)
    check("改动记 prev", r["changed"] is True and r["prev_identity"] == "conviction", str(r))
    check("改动文案", ident.change_note(r) == "10-25 由信念改为投机", str(ident.change_note(r)))

    ident.set_identity(conn, "BBB", "speculative", now=T1)
    cur = ident.current(conn)
    check("current 取每个标的最新一条",
          set(cur) == {"AAA", "BBB"} and cur["AAA"]["identity"] == "speculative"
          and cur["AAA"]["prev_identity"] == "conviction", str(cur))
    h = ident.history(conn, "aaa")
    check("history 新的在前（代码大小写不敏感）",
          [x["identity"] for x in h] == ["speculative", "conviction"], str(h))
    check("changed_at 带时区偏移", h[0]["changed_at"] == "2026-10-25T21:05:00+08:00", h[0]["changed_at"])

    raised = None
    try:
        ident.set_identity(conn, "AAA", "yolo")
    except ValueError as e:
        raised = e
    n = conn.execute("SELECT COUNT(*) FROM holding_identity").fetchone()[0]
    check("非法身份 → ValueError，且不写行", raised is not None and n == 3, f"rows={n}")

    raised = None
    try:
        conn.execute("INSERT INTO holding_identity (symbol, identity, changed_at) VALUES ('X', 'yolo', 't')")
    except Exception as e:  # noqa: BLE001
        raised = e
    check("表的 CHECK 约束兜底", raised is not None, type(raised).__name__ if raised else "None")
    check("change_note(None) → None", ident.change_note(None) is None)
    conn.close()

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过。")

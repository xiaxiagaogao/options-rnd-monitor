"""自选清单后台 worker（watchlist spec §5）：假准入 + 假回填，临时库，不触网、不拉 ThetaData。

    .venv/bin/python tests/test_watchlist_worker.py
"""
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rnd import db, thetalock, watchlist as wl, watchlist_worker as worker

failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        failures.append(name)


PASS = {"ok": True, "verdict": True, "metrics": {"atm_spread_med": 0.015, "n_two_sided_strikes": 30},
        "checks": {"atm_spread_ok": True, "coverage_ok": True},
        "thresholds": {"max_atm_spread": 0.10, "min_two_sided": 10}}
THIN = {"ok": True, "verdict": False, "metrics": {"atm_spread_med": 0.162, "n_two_sided_strikes": 2},
        "checks": {"atm_spread_ok": False, "coverage_ok": False},
        "thresholds": {"max_atm_spread": 0.10, "min_two_sided": 10}}
NOQUOTE = {"ok": True, "verdict": False, "metrics": {"atm_spread_med": None, "n_two_sided_strikes": 12},
           "checks": {"atm_spread_ok": False, "coverage_ok": True},
           "thresholds": {"max_atm_spread": 0.10, "min_two_sided": 10}}

print("[准入未过的原因白话]")
r = worker.reject_reason(THIN)
check("两条都没过都写出来", "16.2%" in r and "10%" in r and "2 个" in r and "至少 10" in r, r)
check("没有双边报价单独说明", "没有双边报价" in worker.reject_reason(NOQUOTE), worker.reject_reason(NOQUOTE))
check("通过 → None", worker.reject_reason(PASS) is None)

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "w.sqlite"
    lock = Path(tmp) / "theta.lock"
    conn = db.get_conn(path)
    for t, role in (("AAA", "conviction"), ("THIN", "speculative"), ("NODATA", "speculative"),
                    ("BOOM", "speculative"), ("GONE", "speculative"), ("OLD", "speculative")):
        wl.add(conn, t, role)
    wl.set_stage(conn, "OLD", "backfilling")          # 上一个 worker 被杀留下的
    conn.close()

    checks = {"AAA": PASS, "THIN": THIN, "NODATA": PASS, "GONE": PASS, "OLD": PASS}
    order = []

    def fake_check(t):
        order.append(("check", t))
        if t == "BOOM":
            raise RuntimeError("ThetaData 超时")
        return checks[t]

    def fake_backfill(t):
        order.append(("backfill", t))
        c = db.get_conn(path)
        if t == "GONE":                                # 回填途中被用户移出清单
            wl.remove(c, "GONE")
        if t in ("AAA", "OLD", "GONE"):
            db.upsert_indicators(c, {"date": "2026-11-06", "symbol": t, "expiry": "2026-11-20",
                                     "dte": 14, "gate_pass": 1, "pinned": 1, "roll": 0})
            c.commit()
        c.close()
        return 0

    print("[一轮处理]")
    done = worker.run(db_path=path, lock_path=lock, check_fn=fake_check, backfill_fn=fake_backfill)
    c = db.get_conn(path)
    st = {e["ticker"]: e for e in wl.entries(c)}
    check("通过 + 回填有数据 → ready", st["AAA"]["stage"] == "ready", st["AAA"]["stage"])
    check("准入没过 → rejected 带原因、存指标", st["THIN"]["stage"] == "rejected"
          and "16.2%" in st["THIN"]["reason"] and st["THIN"]["check"]["metrics"]["n_two_sided_strikes"] == 2)
    check("回填后仍无数据 → error", st["NODATA"]["stage"] == "error" and "无可用数据" in st["NODATA"]["reason"],
          str(st["NODATA"]))
    check("检查抛错 → error 带原因、不回填", st["BOOM"]["stage"] == "error" and "ThetaData 超时" in st["BOOM"]["reason"]
          and ("backfill", "BOOM") not in order)
    check("回填途中被移出 → 不复活", "GONE" not in st)
    check("残留 backfilling 复位后重新处理到 ready", st["OLD"]["stage"] == "ready", st["OLD"]["stage"])
    check("每个标的本轮只处理一次（error 不在同一轮反复重试）",
          [x for x in order if x[0] == "check"].count(("check", "BOOM")) == 1)
    check("返回处理过的标的", sorted(done) == ["AAA", "BOOM", "GONE", "NODATA", "OLD", "THIN"], str(done))
    check("池 = ready", wl.ready_tickers(c) == ["AAA", "OLD"], str(wl.ready_tickers(c)))
    c.close()

    print("[下一轮重试 error]")
    order.clear()
    checks["BOOM"] = PASS
    worker.run(db_path=path, lock_path=lock, check_fn=lambda t: PASS if t == "BOOM" else fake_check(t),
               backfill_fn=fake_backfill)
    retried = sorted({t for _, t in order})
    check("error 的 NODATA 被重试，rejected 的 THIN 不动", "NODATA" in retried and "THIN" not in retried, str(retried))

    print("[ThetaData 锁]")
    held = threading.Event()
    release = threading.Event()

    def holder():
        with thetalock.thetadata_lock(lock):
            held.set()
            release.wait(5)

    th = threading.Thread(target=holder)
    th.start()
    held.wait(5)
    c = db.get_conn(path)
    wl.recheck(c, "THIN")
    c.close()
    t0 = time.time()
    threading.Timer(0.6, release.set).start()
    worker.run(db_path=path, lock_path=lock, check_fn=lambda t: THIN, backfill_fn=fake_backfill)
    waited = time.time() - t0
    th.join()
    check("锁被占时 worker 等到释放才开始", waited >= 0.5, f"waited={waited:.2f}s")
    try:
        with thetalock.thetadata_lock(lock, blocking=False):
            free = True
    except BlockingIOError:
        free = False
    check("worker 结束后锁已释放", free)

print("[锁等待上限 / 回填超时]")
with tempfile.TemporaryDirectory() as tmp2:
    lk = Path(tmp2) / "theta.lock"
    hold, rel = threading.Event(), threading.Event()

    def holder2():
        with thetalock.thetadata_lock(lk):
            hold.set()
            rel.wait(5)

    th2 = threading.Thread(target=holder2)
    th2.start()
    hold.wait(5)
    t0 = time.time()
    timed_out = False
    try:
        with thetalock.thetadata_lock(lk, timeout=0.5, poll=0.1):
            pass
    except TimeoutError:
        timed_out = True
    check("锁被占且超过等待上限 → TimeoutError", timed_out and 0.4 <= time.time() - t0 < 2,
          f"{time.time() - t0:.2f}s")
    rel.set()
    th2.join()
    with thetalock.thetadata_lock(lk, timeout=0.5, poll=0.1):
        check("锁释放后带上限也能拿到", True)

    import subprocess as sp
    calls = []

    def fake_call(args, timeout=None, **kw):
        calls.append(timeout)
        raise sp.TimeoutExpired(args, timeout)

    orig = worker.subprocess.call
    worker.subprocess.call = fake_call
    try:
        rc = worker._backfill_subprocess("ZZZ")
    finally:
        worker.subprocess.call = orig
    check("回填带超时，超时返回非 0 而不是抛出", rc != 0 and calls == [worker.BACKFILL_TIMEOUT_S], f"rc={rc} calls={calls}")

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部通过。")

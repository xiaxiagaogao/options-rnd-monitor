"""自选清单后台 worker（watchlist spec §5）：在 ThetaData 锁内串行推进 queued / error 条目。

    queued → checking → 准入没过：rejected（reason 写哪条没过）
                      → 通过：backfilling → 回填（scripts/backfill.py，可续跑）
                                           → 有 pinned 数据：ready（进池）/ 否则 error（下次 worker 重试）

触发：面板加入 / 重新检查后由 API spawn()；eod_update 结束时若仍有待处理条目也 spawn()。
多个 worker 由锁串行；后到的拿到锁后发现没活就退出。
    .venv/bin/python -m rnd.watchlist_worker
"""
import datetime as dt
import subprocess
import sys

from . import db, watchlist as wl
from .config import OUTPUT_DIR, PROJECT_ROOT
from .thetalock import thetadata_lock

WORKER_LOG = OUTPUT_DIR / "watchlist_worker.log"
BACKFILL_TIMEOUT_S = 3 * 3600   # 单标的 3 年回填实测约 25–30 分钟；卡住 3 小时就杀掉记 error，免得锁被一直占着


def reject_reason(chk: dict) -> str | None:
    """准入没过的白话原因；通过返回 None。"""
    if chk.get("verdict"):
        return None
    m, th, ck = chk.get("metrics") or {}, chk.get("thresholds") or {}, chk.get("checks") or {}
    parts = []
    if not ck.get("atm_spread_ok"):
        sp = m.get("atm_spread_med")
        parts.append("平值附近没有双边报价" if sp is None else
                     f"平值点差 {sp:.1%}（上限 {th.get('max_atm_spread', 0):.0%}）")
    if not ck.get("coverage_ok"):
        parts.append(f"±20% 内双边行权价 {m.get('n_two_sided_strikes')} 个（至少 {th.get('min_two_sided')}）")
    return "；".join(parts) or "准入检查未通过"


def _backfill_subprocess(ticker: str, lock_fd: int | None = None) -> int:
    """跑 scripts/backfill.py。lock_fd：把 ThetaData 锁的 fd 交给子进程——flock 跟着打开的文件走，
    这样 worker 被单独杀掉时，回填子进程仍持有锁，eod / 新 worker 不会和它并发拉 ThetaData。"""
    log = OUTPUT_DIR / f"backfill_watchlist_{ticker}.log"
    log.parent.mkdir(exist_ok=True)
    with open(log, "ab") as fh:
        try:
            return subprocess.call(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "backfill.py"),
                 "--symbols", ticker, "--years", "3"],
                stdout=fh, stderr=subprocess.STDOUT, cwd=PROJECT_ROOT, timeout=BACKFILL_TIMEOUT_S,
                pass_fds=(lock_fd,) if lock_fd is not None else ())
        except subprocess.TimeoutExpired:   # call() 超时会先杀掉子进程再抛
            fh.write(f"\n回填超过 {BACKFILL_TIMEOUT_S // 3600} 小时，已终止\n".encode())
            return -9


def _has_pinned(conn, ticker: str) -> bool:
    return conn.execute("SELECT 1 FROM rnd_indicators WHERE symbol=? AND pinned=1 LIMIT 1",
                        (ticker,)).fetchone() is not None


def _process(conn, t: str, check_fn, backfill_fn):
    """单个标的：checking → 准入 → rejected / backfilling → 回填 → ready / error。"""
    if not wl.set_stage(conn, t, "checking"):
        return
    try:
        chk = check_fn(t)
    except Exception as e:  # noqa: BLE001  单标的出错不拖垮其余
        wl.set_stage(conn, t, "error", reason=f"准入检查出错：{type(e).__name__}: {e}"[:300])
        return
    if not chk.get("ok"):
        wl.set_stage(conn, t, "error", reason=f"准入检查拉数失败：{chk.get('error')}"[:300])
        return
    reason = reject_reason(chk)
    if reason:
        wl.set_stage(conn, t, "rejected", reason=reason, check=chk)
        print(f"[watchlist] {t} 准入未过：{reason}", flush=True)
        return
    if not wl.set_stage(conn, t, "backfilling", check=chk):
        return
    print(f"[watchlist] {t} 准入通过，回填 3 年 …", flush=True)
    rc = backfill_fn(t)   # backfill.py 吞掉单标的异常后照样 0 退出，所以成败看库里有没有数据
    if _has_pinned(conn, t):
        if wl.set_stage(conn, t, "ready"):
            print(f"[watchlist] {t} 就绪，已进池", flush=True)
    else:
        wl.set_stage(conn, t, "error", reason=f"回填后仍无可用数据（回填退出码 {rc}），下次自动重试")


def run(*, db_path=None, lock_path=None, check_fn=None, backfill_fn=None) -> list[str]:
    """处理完所有待处理条目后返回；每个标的本轮最多处理一次（error 留给下一轮重试）。"""
    if check_fn is None:
        from .admission import check_candidate as check_fn
    conn_of = (lambda: db.get_conn(db_path)) if db_path else db.get_conn
    done: list[str] = []
    with thetadata_lock(lock_path) as lock_fh:
        bf = backfill_fn or (lambda t: _backfill_subprocess(t, lock_fd=lock_fh.fileno()))
        conn = conn_of()
        try:
            stale = wl.reset_stale(conn)
            if stale:
                print(f"[watchlist] 复位残留：{stale}", flush=True)
            while True:
                jobs = [t for t in wl.pending_jobs(conn) if t not in done]
                if not jobs:
                    break
                t = jobs[0]
                done.append(t)
                try:
                    _process(conn, t, check_fn, bf)
                except Exception as e:  # noqa: BLE001  任何意外都落到 error，不让条目卡在中间态
                    conn.rollback()
                    wl.set_stage(conn, t, "error", reason=f"处理出错：{type(e).__name__}: {e}"[:300])
        finally:
            conn.close()
    return done


def spawn():
    """起一个脱离父进程的 worker（不等待）。输出追加到 output/watchlist_worker.log。"""
    WORKER_LOG.parent.mkdir(exist_ok=True)
    with open(WORKER_LOG, "ab") as fh:
        fh.write(f"\n=== spawn @ {dt.datetime.now().astimezone().isoformat(timespec='seconds')} ===\n".encode())
        fh.flush()
        subprocess.Popen([sys.executable, "-m", "rnd.watchlist_worker"], stdout=fh,
                         stderr=subprocess.STDOUT, cwd=PROJECT_ROOT, start_new_session=True)


if __name__ == "__main__":
    print("处理完毕：", run() or "无待处理条目", flush=True)

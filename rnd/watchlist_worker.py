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
SINGLE_LOCK = OUTPUT_DIR / "watchlist_worker.lock"   # 同一时间只跑一个 worker（与 ThetaData 锁分开）
BACKFILL_TIMEOUT_S = 3 * 3600   # 单标的 3 年回填实测约 25–30 分钟；卡住 3 小时就杀掉记 error，免得锁被一直占着


def brief_error(e: BaseException) -> str:
    """异常压成给人看的一行：ThetaData 会话被顶掉单独说白话，其余只留第一行并限长。"""
    text = str(e)
    if "Invalid session" in text or "UNAUTHENTICATED" in text:
        return "ThetaData 会话被另一个连接顶掉（免费档只允许单会话），下次自动重试"
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return f"{type(e).__name__}: {first}"[:160]


def _reset_thetadata_session():
    """丢掉本进程缓存的 ThetaData 客户端，下次调用重新登录。
    回填在子进程里会新开一个会话，把 worker 手上的旧会话顶掉（免费档单会话，2026-11-08 生产实测：
    SPCX 回填完，同一 worker 后续 9 个准入检查全报 UNAUTHENTICATED: Invalid session）。"""
    from . import fetch
    fetch._client.cache_clear()


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


def _process(conn, t: str, check_fn, backfill_fn, session_reset):
    """单个标的：checking → 准入 → rejected / backfilling → 回填 → ready / error。"""
    if not wl.set_stage(conn, t, "checking"):
        return
    try:
        session_reset()
        chk = check_fn(t)
    except Exception as e:  # noqa: BLE001  单标的出错不拖垮其余
        wl.set_stage(conn, t, "error", reason=f"准入检查出错：{brief_error(e)}")
        return
    if not chk.get("ok"):
        wl.set_stage(conn, t, "error", reason=f"准入检查拉数失败：{brief_error(RuntimeError(chk.get('error')))}")
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


def run(*, db_path=None, lock_path=None, check_fn=None, backfill_fn=None,
        session_reset=None) -> list[str]:
    """处理完所有待处理条目后返回；每个标的本轮最多处理一次（error 留给下一轮重试）。"""
    if check_fn is None:
        from .admission import check_candidate as check_fn
    session_reset = session_reset or _reset_thetadata_session
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
                    _process(conn, t, check_fn, bf, session_reset)
                except Exception as e:  # noqa: BLE001  任何意外都落到 error，不让条目卡在中间态
                    conn.rollback()
                    wl.set_stage(conn, t, "error", reason=f"处理出错：{brief_error(e)}")
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


def main(*, db_path=None, lock_path=None, single_path=None, **kw) -> list[str] | None:
    """worker 入口：同一时间只跑一个。已有 worker 在跑 → 返回 None 立即退出（新加的条目由它在
    同一轮里接着处理）。跑完放掉单例锁后再看一眼：若恰好在收尾时又有新条目，就再来一轮。"""
    from .thetalock import thetadata_lock as _lock
    done: list[str] = []
    while True:
        try:
            with _lock(single_path or SINGLE_LOCK, blocking=False):
                done += run(db_path=db_path, lock_path=lock_path, **kw)
        except BlockingIOError:
            return None if not done else done
        conn = db.get_conn(db_path) if db_path else db.get_conn()
        try:
            again = wl.pending_jobs(conn)
        finally:
            conn.close()
        if not again or set(again) <= set(done):   # 只剩本轮处理过的 error，留给下次
            return done


if __name__ == "__main__":
    got = main()
    print("已有 worker 在处理，本进程退出" if got is None else f"处理完毕：{got or '无待处理条目'}", flush=True)
